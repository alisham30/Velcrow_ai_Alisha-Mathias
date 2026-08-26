r"""Revenue Lab (spec 9): the same 20 goals, shopped twice.

Once by someone on their own, once with the agent. The scoreboard is the
pitch, so it is computed rather than asserted - and it is computed honestly,
which means showing the coupon money the merchant GIVES AWAY next to the
revenue the agent brings in. A scoreboard that only counts the wins is not
evidence.

Where the lift actually comes from, and where it does not:

  Stock-out rescue   a refused sale becomes a reservation and then a sale.
                     Revenue that would have been zero. This is the real lift.
  Reorder            a past basket comes back at today's prices. A repeat
                     that often would not have happened unprompted.
  Near-miss          the shopper tops up to unlock a coupon. Basket size rises,
                     but the merchant hands back the discount - so it lifts AOV
                     and shopper value, NOT margin. Counted as both here.
  Coupon claiming    pure cost to the merchant. They chose to offer it; the
                     agent just stops the shopper forgetting. Shown as a cost.

Deterministic on purpose: it reads the real catalogs and runs the real coupon
optimiser, but simulates the two shopping paths rather than making 40 live
model calls. It measures the MECHANICS, not the model's wording - so it is
repeatable, and nobody should describe it as 20 live conversations.

    .\.venv\Scripts\python.exe -m lab.revenue_lab
"""
from __future__ import annotations

import sys
from typing import Any

from common.money import rupees
from shop import coupons as coupon_engine
from shop.config import load_catalog, load_config

# 20 scripted goals. `kind` is what happens to a shopper without help:
#   plain     they buy what they came for
#   shortfall they wanted more than the shelf had; the rest is simply lost
#   stockout  the thing is gone; they leave with nothing
#   repeat    they meant to reorder and mostly do not get round to it
GOALS: list[dict[str, Any]] = [
    {"shop": "grocery", "items": [("lemons-1kg", 2)], "kind": "plain"},
    {"shop": "grocery", "items": [("basmati-5kg", 1)], "kind": "plain"},
    {"shop": "grocery", "items": [("atta-5kg", 1), ("toor-dal-1kg", 1)], "kind": "plain"},
    {"shop": "grocery", "items": [("honey-500g", 1), ("lemons-1kg", 2)], "kind": "nearmiss"},
    {"shop": "grocery", "items": [("ghee-500ml", 1)], "kind": "stockout"},
    {"shop": "grocery", "items": [("filter-coffee-250g", 2)], "kind": "plain"},
    {"shop": "grocery", "items": [("infant-formula-400g", 1)], "kind": "repeat"},
    {"shop": "grocery", "items": [("tomatoes-1kg", 3), ("lemons-1kg", 1)], "kind": "plain"},
    {"shop": "grocery", "items": [("basmati-5kg", 1), ("atta-5kg", 1)], "kind": "repeat"},
    {"shop": "grocery", "items": [("honey-500g", 2)], "kind": "nearmiss"},
    {"shop": "apparel", "items": [("kurti-indigo-cotton", 1)], "kind": "plain"},
    {"shop": "apparel", "items": [("tshirt-graphic-black", 2)], "kind": "plain"},
    {"shop": "apparel", "items": [("hoodie-fleece-grey", 1)], "kind": "stockout"},
    {"shop": "apparel", "items": [("kurta-men-linen", 1)], "kind": "stockout"},
    {"shop": "apparel", "items": [("dupatta-chanderi-rose", 1)], "kind": "nearmiss"},
    {"shop": "apparel", "items": [("shirt-oxford-white", 1)], "kind": "plain"},
    {"shop": "apparel", "items": [("kurti-indigo-cotton", 1), ("dupatta-chanderi-rose", 1)],
     "kind": "repeat"},
    {"shop": "apparel", "items": [("socks-crew-3pack", 2)], "kind": "plain"},
    {"shop": "apparel", "items": [("tshirt-graphic-black", 1), ("socks-crew-3pack", 1)],
     "kind": "shortfall"},
    {"shop": "apparel", "items": [("shirt-oxford-white", 8)], "kind": "shortfall"},
]

# How often an unaided shopper follows through, by situation. Stated here
# rather than buried, because these assumptions ARE the comparison and a
# reader should be able to argue with them.
UNAIDED = {
    "plain": 1.0,        # they came to buy it and they buy it
    "nearmiss": 1.0,     # they buy, at the smaller basket, coupon unclaimed
    "shortfall": 1.0,    # they buy what is there and forget the rest
    "stockout": 0.0,     # nothing on the shelf, nothing bought
    "repeat": 0.35,      # they meant to reorder; most months they do not
}
ASSISTED = {
    "plain": 1.0,
    "nearmiss": 1.0,
    "shortfall": 1.0,
    "stockout": 0.55,    # reserved, told when it landed, some come back
    "repeat": 0.80,      # one line brings the basket back
}


def _shop(kind: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    cfg = load_config(kind)
    return cfg, {p["id"]: p for p in load_catalog(cfg)}


def _stock_of(product: dict[str, Any], want: int) -> int:
    if product.get("variants"):
        return max((v["stock"] for v in product["variants"]), default=0)
    return int(product.get("stock", 0))


def _lines(catalog: dict[str, Any], items: list[tuple[str, int]],
           cap_to_stock: bool) -> list[dict[str, Any]]:
    out = []
    for item_id, qty in items:
        p = catalog.get(item_id)
        if p is None:
            continue
        have = _stock_of(p, qty)
        n = min(qty, have) if cap_to_stock else qty
        if n <= 0:
            continue
        out.append({"item_id": item_id, "variant": "", "qty": n,
                    "unit_price_paise": p["price_paise"], "category": p["category"]})
    return out


def _priced(lines: list[dict[str, Any]], coupon_set: list[dict[str, Any]],
            claim: bool) -> dict[str, Any]:
    """What the merchant actually receives, with or without the coupon claimed."""
    result = coupon_engine.evaluate(lines, coupon_set)
    subtotal = result["subtotal_paise"]
    if not claim:
        # The shopper never applied one. The merchant keeps the full amount;
        # the shopper simply pays more than they had to.
        return {"revenue": subtotal, "discount": 0, "codes": [], "near_miss": result["near_miss"]}
    best = result["best"]
    return {"revenue": best["net_total_paise"], "discount": best["discount_paise"],
            "codes": best["codes"], "near_miss": result["near_miss"]}


def run() -> dict[str, Any]:
    shops = {k: _shop(k) for k in ("grocery", "apparel")}
    without = {"orders": 0, "revenue": 0, "discount": 0, "coupon_orders": 0,
               "rescued_orders": 0, "rescued_revenue": 0, "lost_revenue": 0}
    with_agent = dict(without)
    rows: list[dict[str, Any]] = []

    for goal in GOALS:
        cfg, catalog = shops[goal["shop"]]
        kind = goal["kind"]

        # -- on their own -------------------------------------------------
        plain_lines = _lines(catalog, goal["items"], cap_to_stock=True)
        plain = _priced(plain_lines, cfg["coupons"], claim=False)
        buys_alone = UNAIDED[kind] >= 1.0 and bool(plain_lines)
        alone_revenue = plain["revenue"] if buys_alone else 0
        # a repeat that never happens, or a stock-out nobody captured
        alone_expected = int(plain["revenue"] * UNAIDED[kind]) if plain_lines else 0
        if kind in ("stockout", "repeat"):
            alone_revenue = alone_expected

        # -- with the agent -----------------------------------------------
        # It claims coupons, tops up a near-miss, and holds what the shelf
        # could not supply so the sale can still happen later.
        agent_items = list(goal["items"])
        agent_lines = _lines(catalog, agent_items, cap_to_stock=True)
        agent = _priced(agent_lines, cfg["coupons"], claim=True)

        topped_up = 0
        if kind == "nearmiss" and agent["near_miss"]:
            near = agent["near_miss"]
            topped_up = near["add_paise"]
            # the shopper adds the cheapest thing that closes the gap
            filler = min(catalog.values(), key=lambda p: p["price_paise"])
            units = max(1, -(-topped_up // filler["price_paise"]))
            agent_lines = agent_lines + [{
                "item_id": filler["id"], "variant": "", "qty": units,
                "unit_price_paise": filler["price_paise"], "category": filler["category"]}]
            agent = _priced(agent_lines, cfg["coupons"], claim=True)

        full_lines = _lines(catalog, agent_items, cap_to_stock=False)
        unmet = sum(l["qty"] * l["unit_price_paise"] for l in full_lines) - sum(
            l["qty"] * l["unit_price_paise"] for l in _lines(catalog, agent_items, True))

        agent_revenue = int(agent["revenue"] * ASSISTED[kind]) if agent_lines else 0
        if kind == "stockout":
            # nothing was on the shelf, so the whole basket is a rescue
            agent_revenue = int(
                sum(l["qty"] * l["unit_price_paise"] for l in full_lines) * ASSISTED[kind])
        rescued = agent_revenue if kind in ("stockout", "shortfall") and agent_revenue > alone_revenue else 0

        without["orders"] += 1 if alone_revenue else 0
        without["revenue"] += alone_revenue
        without["lost_revenue"] += max(0, unmet) if kind in ("stockout", "shortfall") else 0
        with_agent["orders"] += 1 if agent_revenue else 0
        with_agent["revenue"] += agent_revenue
        with_agent["discount"] += agent["discount"] if agent_revenue else 0
        with_agent["coupon_orders"] += 1 if (agent_revenue and agent["codes"]) else 0
        if rescued:
            with_agent["rescued_orders"] += 1
            with_agent["rescued_revenue"] += rescued - alone_revenue

        rows.append({
            "shop": cfg["brand"], "kind": kind,
            "items": ", ".join(f"{i}x{q}" for i, q in goal["items"]),
            "alone_paise": alone_revenue, "agent_paise": agent_revenue,
            "coupons": agent["codes"], "topped_up_paise": topped_up,
        })

    for side in (without, with_agent):
        side["aov"] = side["revenue"] // side["orders"] if side["orders"] else 0
    lift = with_agent["revenue"] - without["revenue"]

    return {
        "goals": len(GOALS), "rows": rows,
        "without": without, "with_agent": with_agent,
        "lift_paise": lift,
        "lift_pct": round(lift / without["revenue"] * 100, 1) if without["revenue"] else 0.0,
        "assumptions": {"unaided": UNAIDED, "assisted": ASSISTED},
        "method": ("Deterministic simulation over the real catalogs and the real coupon "
                   "optimiser. It measures the mechanics, not the model's wording, and is "
                   "not 20 live model conversations."),
    }


def main() -> int:
    r = run()
    w, a = r["without"], r["with_agent"]
    out = sys.stdout

    print(f"\nRevenue Lab - {r['goals']} scripted goals, shopped twice\n", file=out)
    print(f"  {'':22} {'on their own':>16} {'with VelcrowAI':>16}", file=out)
    print(f"  {'-' * 56}", file=out)
    print(f"  {'orders':22} {w['orders']:>16} {a['orders']:>16}", file=out)
    print(f"  {'revenue':22} {rupees(w['revenue']):>16} {rupees(a['revenue']):>16}", file=out)
    print(f"  {'average order':22} {rupees(w['aov']):>16} {rupees(a['aov']):>16}", file=out)
    print(f"  {'orders with a coupon':22} {w['coupon_orders']:>16} {a['coupon_orders']:>16}",
          file=out)
    print(f"  {'discount given away':22} {rupees(w['discount']):>16} {rupees(a['discount']):>16}",
          file=out)
    print(f"  {'sales rescued':22} {w['rescued_orders']:>16} {a['rescued_orders']:>16}", file=out)
    print(f"  {'rescued revenue':22} {rupees(w['rescued_revenue']):>16} "
          f"{rupees(a['rescued_revenue']):>16}", file=out)
    print(f"\n  Net lift {rupees(r['lift_paise'])} ({r['lift_pct']:+}%)", file=out)
    print(f"  Of which rescued stock-outs: {rupees(a['rescued_revenue'])}", file=out)
    print(f"  Coupon margin handed back:   {rupees(a['discount'])}", file=out)
    print(f"\n  {r['method']}\n", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
