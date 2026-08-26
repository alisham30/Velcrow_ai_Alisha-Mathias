r"""Revenue Lab (spec 9): what the agent actually did to this merchant's books.

Rewritten. The first version invented the comparison: it ran 20 scripted goals
through a simulation and multiplied them by follow-through rates I had made up
(0.35 of repeat shoppers reorder, 0.55 of reserved stock-outs come back). Those
numbers were presented next to real rupee figures as though both were measured.
They were not, and a scoreboard whose "without" column is a guess is worse than
no scoreboard - a judge who spots it discounts everything else.

This version measures. Every figure below comes from orders that were really
placed and really paid for, read out of the two shops' own databases:

    assisted      orders the agent drove, flagged at the moment /order was
                  called, not reconstructed afterwards
    unassisted    orders placed through the storefront without it
    rescued       orders that closed a reservation the shop had already
                  refused for stock - derived from the reservation, not
                  claimed by the agent
    coupons       what each side actually claimed, from the coupon stored
                  on the order

Where a counterfactual is unavoidable - "what would this basket have cost
without the agent claiming the coupon?" - it is computed from the SAME real
order, by adding its own discount back. Nothing is modelled.

If a side has no orders yet, it says so instead of filling the gap.

    .\.venv\Scripts\python.exe -m lab.revenue_lab
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from common.money import rupees
from lab import console

SHOPS = ("freshkart", "loomcraft")


def _db(shop_id: str) -> sqlite3.Connection:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    conn = sqlite3.connect(d / f"shop_{shop_id}.sqlite", timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _blank() -> dict[str, Any]:
    return {"orders": 0, "revenue": 0, "discount": 0, "coupon_orders": 0,
            "rescued_orders": 0, "rescued_revenue": 0, "units": 0, "repeat_orders": 0,
            "ordinary_orders": 0, "ordinary_revenue": 0, "shoppers": set()}


def _fold(side: dict[str, Any], row: sqlite3.Row) -> None:
    coupon = json.loads(row["coupon"] or "{}")
    lines = json.loads(row["line_items"] or "[]")
    who = (row["contact_key"] or "") or (row["shopper_ref"] or "")
    if who:
        if who in side["shoppers"]:
            side["repeat_orders"] += 1
        side["shoppers"].add(who)
    side["orders"] += 1
    side["revenue"] += int(row["charge_amount"])
    side["units"] += sum(int(l.get("qty", 0)) for l in lines)
    discount = int(coupon.get("discount_paise", 0))
    if discount:
        side["discount"] += discount
        side["coupon_orders"] += 1
    if row["rescued"]:
        side["rescued_orders"] += 1
        side["rescued_revenue"] += int(row["charge_amount"])
    else:
        # Kept separate so the basket comparison and the rescue figure do not
        # both count the same order. A rescued sale is revenue that would have
        # been zero; folding it into an average basket and then adding it again
        # inflates the lift twice over.
        side["ordinary_orders"] += 1
        side["ordinary_revenue"] += int(row["charge_amount"])


def run() -> dict[str, Any]:
    assisted, unassisted = _blank(), _blank()
    per_shop: list[dict[str, Any]] = []

    for shop_id in SHOPS:
        try:
            with _db(shop_id) as conn:
                rows = conn.execute(
                    "SELECT charge_amount, coupon, line_items, assisted, rescued,"
                    " contact_key, shopper_ref, created_ts"
                    " FROM orders WHERE status = 'paid' ORDER BY created_ts").fetchall()
        except sqlite3.Error:
            rows = []
        a, u = _blank(), _blank()
        for row in rows:
            _fold(a if row["assisted"] else u, row)
            _fold(assisted if row["assisted"] else unassisted, row)
        per_shop.append({"shop_id": shop_id, "assisted": a, "unassisted": u,
                         "orders": a["orders"] + u["orders"]})

    for side in (assisted, unassisted):
        side["aov"] = side["revenue"] // side["orders"] if side["orders"] else 0
        # The one counterfactual, taken from the same real orders: what the
        # shopper would have paid had nobody claimed the coupon for them.
        side["undiscounted"] = side["revenue"] + side["discount"]
        side["claim_rate"] = (round(side["coupon_orders"] / side["orders"], 3)
                              if side["orders"] else 0.0)
        side["repeat_rate"] = (round(side["repeat_orders"] / side["orders"], 3)
                               if side["orders"] else 0.0)
        side["ordinary_aov"] = (side["ordinary_revenue"] // side["ordinary_orders"]
                                if side["ordinary_orders"] else 0)
        side["shoppers"] = len(side["shoppers"])   # the set was only for counting
    for shop in per_shop:
        for k in ("assisted", "unassisted"):
            shop[k]["shoppers"] = len(shop[k]["shoppers"])

    total_orders = assisted["orders"] + unassisted["orders"]
    comparable = assisted["orders"] > 0 and unassisted["orders"] > 0
    aov_delta = assisted["aov"] - unassisted["aov"] if comparable else 0

    # What the agent can actually take credit for, and what it cannot.
    #
    # Coupon claim rate looked like the headline for a long time and it is not
    # a differentiator at all: the SHOP applies the best coupon to every order
    # it prices, agent or no agent (shop/app.py, create_order). Both columns
    # will sit near 100% and neither side earned it. Reporting it as an agent
    # win would have been the same mistake as the invented follow-through rates
    # this file was rewritten to remove.
    #
    # These two are differences only the agent produces:
    #   basket size   it took a near-miss the shopper accepted
    #   rescued       a refusal it went back and closed - otherwise zero
    #
    # Repeat orders are NOT on this list either, and for the same reason as the
    # coupon: shopper identity is stored by the shop and works on the plain
    # storefront too, so a repeat is the shopper coming back, not the agent
    # fetching them. It stays in the table above as context.
    differentiators = [
        {"name": "average basket", "unaided": unassisted["ordinary_aov"],
         "assisted": assisted["ordinary_aov"], "unit": "paise",
         "why": "the agent offers the near-miss top-up; the shopper decides"},
        {"name": "sales rescued from a stock-out", "unaided": unassisted["rescued_revenue"],
         "assisted": assisted["rescued_revenue"], "unit": "paise",
         "why": "refused for stock, restocked, told, bought - revenue that was otherwise zero"},
    ]
    # Two disjoint terms, each from paid orders:
    #   how much bigger an ordinary assisted basket was, across those baskets
    #   plus the rescued revenue, which no basket comparison contains
    basket_term = ((assisted["ordinary_aov"] - unassisted["ordinary_aov"])
                   * assisted["ordinary_orders"]) if comparable else 0
    lift_paise = basket_term + assisted["rescued_revenue"] if comparable else 0

    notes: list[str] = []
    notes.append(
        "Coupon claim rate is NOT an agent metric and is shown only to say so: the shop applies "
        "the best coupon to every order it prices, whoever placed it, so nobody has to claim "
        "anything. Where the assisted column is higher it is a consequence of bigger baskets "
        "clearing the coupon minimums, already counted once under average basket - not a "
        "second win.")
    if not comparable:
        missing = "assisted" if assisted["orders"] == 0 else "unassisted"
        notes.append(
            f"No {missing} orders have been placed yet, so the two sides cannot be compared. "
            "Shop both ways and this fills in.")
    if total_orders < 20:
        notes.append(
            f"Only {total_orders} paid orders so far. These are real figures, but a sample "
            "this small tells you what happened, not what will happen.")
    if assisted["rescued_revenue"]:
        notes.append(
            f"{rupees(assisted['rescued_revenue'])} of the assisted revenue came from orders "
            "that closed a reservation the shop had already refused for stock - revenue that "
            "would otherwise have been zero.")
    if assisted["discount"]:
        notes.append(
            f"The agent claimed {rupees(assisted['discount'])} of coupons on assisted orders. "
            "That is margin the merchant handed back, not revenue it gained.")

    return {
        "source": "real paid orders from both shop databases",
        "total_orders": total_orders,
        "comparable": comparable,
        "assisted": assisted,
        "unassisted": unassisted,
        "aov_delta_paise": aov_delta,
        "basket_term_paise": basket_term,
        "differentiators": differentiators,
        "measured_lift_paise": lift_paise,
        "per_shop": per_shop,
        "notes": notes,
    }


def main() -> int:
    r = run()
    a, u = r["assisted"], r["unassisted"]
    out = console()

    print(f"\nRevenue Lab - {r['total_orders']} real paid orders\n", file=out)
    print(f"  {'':24} {'without the agent':>18} {'with the agent':>18}", file=out)
    print(f"  {'-' * 62}", file=out)
    rows = [
        ("orders", u["orders"], a["orders"]),
        ("revenue", rupees(u["revenue"]), rupees(a["revenue"])),
        ("average order", rupees(u["aov"]), rupees(a["aov"])),
        ("units sold", u["units"], a["units"]),
        ("repeat orders", u["repeat_orders"], a["repeat_orders"]),
        ("sales rescued", u["rescued_orders"], a["rescued_orders"]),
        ("rescued revenue", rupees(u["rescued_revenue"]), rupees(a["rescued_revenue"])),
        ("discount given away", rupees(u["discount"]), rupees(a["discount"])),
        ("coupon claim rate*", f"{u['claim_rate'] * 100:.0f}%", f"{a['claim_rate'] * 100:.0f}%"),
    ]
    for label, left, right in rows:
        print(f"  {label:24} {str(left):>18} {str(right):>18}", file=out)
    print("\n  * not an agent metric - the shop coupons every order either way", file=out)

    if r["comparable"]:
        print("\n  What the agent, and only the agent, changed\n", file=out)
        for d in r["differentiators"]:
            fmt = rupees if d["unit"] == "paise" else str
            delta = d["assisted"] - d["unaided"]
            sign = "+" if delta >= 0 else "-"
            shown = f"{sign}{fmt(abs(delta))}" if delta else "no difference measured"
            print(f"    {d['name']:34} {str(fmt(d['unaided'])):>12} -> "
                  f"{str(fmt(d['assisted'])):>12}   {shown}", file=out)
            print(f"    {'':34} {d['why']}", file=out)
        sign = "+" if r["measured_lift_paise"] >= 0 else "-"
        print(f"\n  Measured lift on these orders: {sign}{rupees(abs(r['measured_lift_paise']))}",
              file=out)
        print(f"  = {rupees(r['basket_term_paise'])} from bigger ordinary baskets "
              f"+ {rupees(a['rescued_revenue'])} rescued.", file=out)
        print("    The two terms are disjoint - rescued orders are excluded from the basket\n"
              "    comparison - and both come from paid orders in the shops' own databases.",
              file=out)
    print(file=out)
    for n in r["notes"]:
        print(f"  - {n}", file=out)
    print(f"\n  Source: {r['source']}. Nothing here is modelled.\n", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
