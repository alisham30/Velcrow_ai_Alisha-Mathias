r"""A stranger's buying agent (spec 6.6).

This client has never seen either shop. It imports nothing from `agent/`, hard
codes no merchant name, and knows no endpoint that the merchant's own manifest
did not tell it. It reads /.well-known/agent-commerce.json, negotiates
capabilities, discovers the catalog, presents a mandate, orders, confirms
payment, and branches on the typed error codes.

Run it unchanged against either shop:

  .\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8001
  .\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8002

Be precise about what this is: a deterministic protocol client, NOT an agent.
It proves the API contract is open - that a buyer needs no VelcrowAI code and
no private knowledge to transact. The agents in this project are the shopper
agent, the consumer buyer agent and the merchant growth agent.

The mandate comes from the BUYER's own authorization service (--trust), which
is the buyer's side of the arrangement: a merchant never issues the permission
to spend against itself.
"""
from __future__ import annotations

import argparse
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser(description="Buy from any shop that publishes the manifest")
    ap.add_argument("shop_url", help="base URL of a shop that publishes the manifest")
    ap.add_argument("--trust", default="http://127.0.0.1:8003",
                    help="the buyer's own mandate issuer")
    ap.add_argument("--budget", type=int, default=100000, help="ceiling in paise")
    ap.add_argument("--want", default="", help="substring of the product name to prefer")
    ap.add_argument("--size", default="",
                    help="variant label to insist on, whatever this merchant calls variants")
    args = ap.parse_args()
    out = sys.stdout

    with httpx.Client(timeout=20) as http:
        # 1. discovery - the only thing known in advance is the well-known path
        m = http.get(f"{args.shop_url}/.well-known/agent-commerce.json").json()
        merchant, order = m["merchant"], m["order"]
        print(f"discovered: {merchant['name']} ({merchant['category']}), "
              f"pays in {m['currency']}, speaks {order['protocol']}", file=out)

        # 2. capability negotiation - adapt to the merchant instead of assuming
        caps = http.post(f"{args.shop_url}/agent/capabilities",
                         json={"capabilities": {"reservations": True, "discounts": True}}
                         ).json()["capabilities"]
        print(f"capabilities: variants={caps.get('variants')} "
              f"reservations={bool(caps.get('reservations'))} "
              f"discounts={bool(caps.get('discounts'))}", file=out)

        # 3. catalog, from wherever the manifest says it lives
        catalog = http.get(f"{args.shop_url}{m['catalog']}").json()
        affordable = [p for p in catalog if p["price_paise"] <= args.budget]
        if args.want:
            affordable = [p for p in affordable
                          if args.want.lower() in p["name"].lower()] or affordable
        if not affordable:
            print("nothing within budget", file=out)
            return 1
        pick = min(affordable, key=lambda p: p["price_paise"])
        variants = pick.get("variants") or []
        variant = ""
        if variants:
            live = [v for v in variants if v["stock"] > 0]
            # An insisted-on size is honoured even when it is out - being told
            # OUT_OF_STOCK and recovering is better than quietly buying the
            # wrong thing.
            variant = (args.size or (live or variants)[0]["label"])
        print(f"chose {pick['name']}"
              + (f" [{variant}]" if variant else "")
              + f" at {pick['price_paise']} paise", file=out)

        # 4. the buyer's own mandate, presented the way the manifest asks
        token = http.post(f"{args.trust}/mandate",
                          json={"shops": [merchant["id"]], "max_total_paise": args.budget,
                                "max_per_txn_paise": args.budget}).json()["token"]
        auth = {"Authorization": f"Mandate {token}"}

        # 5. basket -> order, recovering from whatever the shop objects to
        cart = http.post(f"{args.shop_url}/cart", json={}).json()["cart_id"]
        line = http.patch(f"{args.shop_url}/cart/{cart}",
                          json={"op": "add", "item_id": pick["id"], "variant": variant, "qty": 1})
        if line.status_code >= 400:
            err = line.json()
            # Recover only in a way this merchant said it supports: the reserve
            # path comes out of the manifest, and is null at a shop that takes
            # no reservations, so there is nothing to call there.
            reserve_at = (order.get("reserve") or "").split(" ")[-1]
            if (err.get("code") == "OUT_OF_STOCK" and reserve_at
                    and "RESERVE" in err.get("available_actions", [])):
                res = http.post(f"{args.shop_url}{reserve_at}",
                                json={"item_id": pick["id"], "variant": variant, "qty": 1,
                                      "contact_ref": "third-party-buyer@example.com"},
                                headers=auth)
                res.raise_for_status()
                print(f"OUT_OF_STOCK -> reserved instead ({res.json()['res_id']}), "
                      f"back {err.get('restock_date', 'unknown')}", file=out)
                return 0
            print(f"refused: {err.get('code')} - {err.get('why')}", file=out)
            return 1

        placed = http.post(f"{args.shop_url}/order", json={"cart_id": cart},
                           headers={**auth, "Idempotency-Key": f"tpb-{cart}"})
        if placed.status_code >= 400:
            err = placed.json()
            print(f"order refused: {err.get('code')} - {err.get('why')}", file=out)
            return 1
        placed = placed.json()
        print(f"quoted {placed['charge_amount']} paise"
              + (f", coupons {placed['coupon']['codes']}" if placed.get("coupon", {}).get("codes")
                 else ", no coupon")
              + f", txn {placed['txn_ref']}", file=out)

        # 6. settle, then verify against the merchant's own record
        done = http.post(f"{args.shop_url}/confirm-payment",
                         json={"txn_ref": placed["txn_ref"],
                               "razorpay_order_id": "order_third_party_demo",
                               "payment_ref": "pay_third_party_demo"},
                         headers={"Idempotency-Key": f"tpb-confirm-{placed['txn_ref']}"})
        if done.status_code >= 400:
            err = done.json()
            print(f"confirm refused: {err.get('code')} - {err.get('why')}", file=out)
            return 1
        final = http.get(f"{args.shop_url}/order/{placed['txn_ref']}").json()
        print(f"PAID {final['charge_amount']} paise at {merchant['name']}, "
              f"status {final['status']}", file=out)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
