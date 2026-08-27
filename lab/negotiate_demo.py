r"""Three negotiations, end to end, against the running services (phase 11).

Two autonomous agents with opposed interests - the buyer capped by a shopper's
ceiling, the merchant floored by its own margin - and every price decision in
code on both sides. Three ceilings, three different endings:

  comfortable   the shop's counter lands inside the ceiling; the buyer takes
                the signed token and pays exactly that number
  tight         the counter is above the ceiling; the buyer stands its ground
                twice and the shop's deal-closing rule takes the ceiling
  impossible    the ceiling is below the merchant's floor; refused with the
                floor named, the buyer walks, nothing is charged

Every step of all three is on both hash chains under its negotiation id:

    .\.venv\Scripts\python.exe -m lab.negotiate_demo
"""
from __future__ import annotations

import sys

import httpx

from common import money
from lab import console

AGENT = "http://127.0.0.1:8003"


def run_one(http: httpx.Client, label: str, ceiling_paise: int, out) -> None:
    print(f"\n--- {label}: ceiling {money.rupees(ceiling_paise)}/unit ---", file=out)
    r = http.post(f"{AGENT}/buyer/negotiate",
                  json={"shop": "apparel", "item_id": "kurti-indigo-cotton",
                        "variant": "M", "qty": 1, "ceiling_paise": ceiling_paise,
                        "contact": "negotiator-demo@example.com"}, timeout=60)
    r.raise_for_status()
    result = r.json()
    for step in result["story"]:
        print(f"  {step['event']:32} {step['why']}", file=out)
    if result["outcome"] == "bought":
        print(f"  => BOUGHT at {money.rupees(result['unit_price_paise'])}/unit, "
              f"order {result['txn_ref']}", file=out)
    else:
        print("  => WALKED, nothing charged", file=out)
    print(f"  both sides' record:  {AGENT}/audit/negotiation/{result['neg_id']}", file=out)


def main() -> int:
    out = console()
    try:
        httpx.get(f"{AGENT}/health", timeout=5)
    except Exception:
        print("The VelcrowAI service is not answering on :8003. Start everything with "
              ".\\run_all.ps1 first.", file=out)
        return 1

    print("\nAgent-to-agent negotiation - Loomcraft's Indigo Cotton Kurti, "
          "list ₹1,499.00", file=out)
    with httpx.Client(timeout=90) as http:
        run_one(http, "comfortable ceiling", 130_000, out)
        run_one(http, "tight ceiling", 110_000, out)
        run_one(http, "impossible ceiling", 60_000, out)
    print("\nEvery price above came from code - the merchant's floor and target from "
          "its cost\nbook, the buyer's moves from its published strategy. "
          "No model touched a number.\n", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
