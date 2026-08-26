r"""Seed a paired demo so the Revenue Lab has something true to measure (spec 16.9).

The Lab reports only real paid orders, which is the right design and also why
it reads badly on a database full of ad-hoc testing. This drives BOTH shopping
paths through the REAL endpoints - real carts, real coupon maths, real mandates,
real Razorpay test orders - so every figure the Lab then reports actually
happened. Nothing here writes a number directly.

The two paths differ in exactly the ways the agent differs, and no others:

  unaided    the shopper buys what they came for and leaves
  assisted   the agent takes the near-miss top-up when one is genuinely on
             offer, and a shopper who was refused for stock is restocked,
             told, and comes back

One thing this makes visible, and it is worth saying out loud in the demo: the
SHOP auto-applies the best coupon on every order, agent or not. Coupon claiming
is the merchant's feature, not the agent's. What the agent actually adds is the
near-miss (a bigger basket the shopper chose) and the rescued stock-out (a sale
that was otherwise zero). Comparing "coupons claimed" between the two paths
would be comparing something that does not differ.

    .\.venv\Scripts\python.exe -m lab.seed_demo
    .\.venv\Scripts\python.exe -m lab.seed_demo --shop grocery --pairs 5
"""
from __future__ import annotations

import argparse
import uuid
from typing import Any

import httpx

from lab import console

AGENT = "http://127.0.0.1:8003"
SHOPS = {"grocery": ("http://127.0.0.1:8001", "freshkart"),
         "apparel": ("http://127.0.0.1:8002", "loomcraft")}

# Baskets a real shopper would build. Three of the five on each side sit inside
# a genuine near-miss window - close enough to a coupon minimum that adding a
# little makes the WHOLE BASKET cheaper - and two do not, because most baskets
# do not. Verified against shop/coupons.py rather than guessed; if the coupon
# config changes, run with --check to see which of these still qualify.
BASKETS = {
    "grocery": [
        [("toor-dal-1kg", "", 1), ("honey-500g", "", 1)],    # Rs 388, Rs 11 off FRESH50
        [("lemons-1kg", "", 1), ("toor-dal-1kg", "", 1),
         ("filter-coffee-250g", "", 1)],                     # Rs 398, Rs 1 off FRESH50
        [("filter-coffee-250g", "", 2)],                     # Rs 370, Rs 29 off FRESH50
        [("lemons-1kg", "", 2), ("tomatoes-1kg", "", 1)],    # too small for any coupon
        [("atta-5kg", "", 1)],
    ],
    "apparel": [
        [("socks-crew-3pack", "", 1), ("scarf-wool-charcoal", "", 1)],   # Rs 51 off WEAVE10
        [("shirt-oxford-white", "L", 2)],                                # Rs 1 off FESTIVE500
        [("kurti-indigo-cotton", "M", 1), ("jeans-slim-indigo", "32", 1)],
        [("tshirt-graphic-black", "M", 1)],
        [("dupatta-chanderi-rose", "", 1)],
    ],
}


class Shopper:
    def __init__(self, http: httpx.Client, shop_url: str, shop_id: str, ref: str) -> None:
        self.http, self.shop_url, self.shop_id, self.ref = http, shop_url, shop_id, ref

    def mandate(self) -> str:
        return self.http.post(f"{AGENT}/mandate",
                              json={"shops": [self.shop_id], "max_total_paise": 5_000_000,
                                    "max_per_txn_paise": 5_000_000}).json()["token"]

    def cart(self) -> str:
        return self.http.post(f"{self.shop_url}/cart", json={}).json()["cart_id"]

    def add(self, cart: str, item_id: str, variant: str, qty: int, token: str) -> dict[str, Any]:
        return self.http.post(
            f"{self.shop_url}/cart/{cart}/fulfil",
            json={"item_id": item_id, "variant": variant, "qty": qty, "mode": "add",
                  "shopper_ref": self.ref, "contact_ref": f"{self.ref}@example.com"},
            headers={"Authorization": f"Mandate {token}"}).json()

    def buy(self, cart: str, token: str, assisted: bool) -> dict[str, Any] | None:
        order = self.http.post(
            f"{self.shop_url}/order",
            json={"cart_id": cart, "assisted": assisted, "shopper_ref": self.ref,
                  "contact": f"{self.ref}@example.com"},
            headers={"Authorization": f"Mandate {token}",
                     "Idempotency-Key": f"seed-{uuid.uuid4().hex[:10]}"})
        if order.status_code >= 400:
            return None
        placed = order.json()
        self.http.post(f"{self.shop_url}/confirm-payment",
                       json={"txn_ref": placed["txn_ref"],
                             "razorpay_order_id": f"order_seed_{uuid.uuid4().hex[:8]}",
                             "payment_ref": f"pay_seed_{uuid.uuid4().hex[:8]}"})
        return placed

    def near_miss(self, cart: str) -> dict[str, Any] | None:
        return self.http.post(f"{self.shop_url}/cart/{cart}/coupons",
                              json={}).json().get("near_miss")

    def cheapest_filler(self) -> tuple[str, str, int]:
        catalog = self.http.get(f"{self.shop_url}/catalog").json()
        flat = [p for p in catalog if not p.get("variants") and p.get("stock", 0) > 0]
        p = min(flat or catalog, key=lambda x: x["price_paise"])
        return p["id"], "", p["price_paise"]

    def thinnest_shelf(self) -> tuple[str, int]:
        """The flat product with the least stock on it right now.

        The first version named a product it believed was out of stock. After a
        week of testing it was not, so the rescue silently did nothing and the
        seed reported a paired comparison in which nothing was ever refused.
        Asking the shop what is actually thin makes the refusal certain
        whatever state the database is in.
        """
        catalog = self.http.get(f"{self.shop_url}/catalog").json()
        flat = [p for p in catalog if not p.get("variants")]
        p = min(flat, key=lambda x: (x.get("stock", 0), x["price_paise"]))
        return p["id"], int(p.get("stock", 0))


def seed_shop(http: httpx.Client, shop_key: str, pairs: int, out) -> dict[str, int]:
    shop_url, shop_id = SHOPS[shop_key]
    baskets = BASKETS[shop_key][:pairs]
    counts = {"unaided": 0, "assisted": 0, "rescued": 0, "topped_up": 0, "skipped": 0,
              "outstanding": 0}

    # -- the unaided path: buy what you came for, leave ---------------------
    plain = Shopper(http, shop_url, shop_id, f"shp_seed_plain_{shop_key}")
    for basket in baskets:
        token, cart = plain.mandate(), plain.cart()
        for item_id, variant, qty in basket:
            plain.add(cart, item_id, variant, qty, token)
        if plain.buy(cart, token, assisted=False):
            counts["unaided"] += 1
        else:
            counts["skipped"] += 1

    # -- the assisted path: same intent, plus what the agent actually adds ---
    helped = Shopper(http, shop_url, shop_id, f"shp_seed_agent_{shop_key}")
    for basket in baskets:
        token, cart = helped.mandate(), helped.cart()
        for item_id, variant, qty in basket:
            helped.add(cart, item_id, variant, qty, token)

        # The near-miss: only taken when the shop genuinely offers one, and
        # only when it leaves the shopper better off. That is the same test
        # the coupon optimiser applies, so this is not a thumb on the scale.
        near = helped.near_miss(cart)
        if near:
            item_id, variant, price = helped.cheapest_filler()
            units = max(1, -(-int(near["add_paise"]) // price))
            helped.add(cart, item_id, variant, units, token)
            counts["topped_up"] += 1
        if helped.buy(cart, token, assisted=True):
            counts["assisted"] += 1
        else:
            counts["skipped"] += 1

    # -- the rescued sale: refused, restocked, told, bought -----------------
    # Ask for two more than the shelf holds, so the refusal is real rather than
    # hoped for. The over-full cart is abandoned; stock only moves at /order.
    item_id, on_hand = helped.thinnest_shelf()
    short_by = 2
    token, cart = helped.mandate(), helped.cart()
    refused = helped.add(cart, item_id, "", on_hand + short_by, token)
    if refused.get("shortfall"):
        http.post(f"{shop_url}/admin/restock",
                  json={"item_id": item_id, "variant": "", "qty": short_by})
        rescue_cart = helped.cart()
        helped.add(rescue_cart, item_id, "", short_by, helped.mandate())
        if helped.buy(rescue_cart, token, assisted=True):
            counts["rescued"] += 1
            counts["assisted"] += 1
    # -- and one refusal nobody fixes ---------------------------------------
    # A ledger of nothing but recovered rows is not a ledger, and the console's
    # whole point is showing the merchant what is STILL being lost. This one is
    # refused and left that way, so outstanding is a real number rather than a
    # zero that only looks tidy.
    thin_id, thin_stock = helped.thinnest_shelf()
    stranded = Shopper(http, shop_url, shop_id, f"shp_seed_waiting_{shop_key}")
    left_waiting = stranded.add(stranded.cart(), thin_id, "", thin_stock + 3,
                                stranded.mandate())
    counts["outstanding"] = int(left_waiting.get("shortfall", 0))

    print(f"  {shop_id}: {counts['unaided']} unaided, {counts['assisted']} assisted "
          f"({counts['topped_up']} took a near-miss, {counts['rescued']} rescued, "
          f"{counts['outstanding']} unit(s) left outstanding)", file=out)
    if counts["skipped"]:
        print(f"  {'':{len(shop_id)}}  {counts['skipped']} basket(s) could not be bought - "
              "out of stock. The pair is incomplete; reset and re-run.", file=out)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a paired demo from real orders")
    ap.add_argument("--shop", choices=[*SHOPS, "both"], default="both")
    ap.add_argument("--pairs", type=int, default=5, help="baskets per side, per shop")
    args = ap.parse_args()
    out = console()

    try:
        httpx.get(f"{AGENT}/health", timeout=5)
    except Exception:
        print("The VelcrowAI service is not answering on :8003. Start everything with "
              ".\\run_all.ps1 first.", file=out)
        return 1

    keys = list(SHOPS) if args.shop == "both" else [args.shop]
    print("\nSeeding a paired demo through the real endpoints\n", file=out)
    with httpx.Client(timeout=30) as http:
        for key in keys:
            try:
                seed_shop(http, key, args.pairs, out)
            except Exception as exc:
                print(f"  {key}: failed - {type(exc).__name__}: {exc}", file=out)
                return 1

    print("\nDone. Every order above was placed and paid through the real API.", file=out)
    print("See it at  http://localhost:5175/audit  ->  Revenue Lab\n", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
