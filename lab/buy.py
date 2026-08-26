r"""Phase 1 buyer harness: drives a purchase against a shop the way the :8003
agent service will in later phases — cart via HTTP, human approval at the
terminal, then wallet.pay (the only path to money) and /confirm-payment.

Full flow (PowerShell):
  .\.venv\Scripts\python.exe -m lab.buy http://localhost:8001 --item lemons-1kg::2 --item honey-500g::2

Pay for an order you created with curl:
  .\.venv\Scripts\python.exe -m lab.buy http://localhost:8001 --pay-only --txn-ref txn_... --mandate <jwt>

Failure demos:
  --forged     tamper the mandate signature before paying (refused, logged)
  --over-cap   pay under a mandate whose caps are below the charge (refused, logged)
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import httpx

from lab import console
from dotenv import load_dotenv

from common import approval, chainlog, errors, mandate, wallet


def _rupees(paise: int) -> str:
    return f"Rs {paise // 100}.{paise % 100:02d}"


def _die_with_chains(shop_id: str, exc: errors.VelcrowError) -> None:
    print(f"\nREFUSED [{exc.code}]: {exc.why}")
    for actor in ("buyer", shop_id):
        entry = chainlog.tail(actor, 1)
        if entry:
            e = entry[0]
            print(f"  {actor} chain entry #{e['i']} [{e['event']}]: {e['why']}")
    sys.exit(2)


def main() -> None:
    console()   # rupee signs survive a cp1252 terminal
    load_dotenv()
    ap = argparse.ArgumentParser(description="Buyer-side purchase harness (all money integer paise)")
    ap.add_argument("shop_url", help="e.g. http://localhost:8001")
    ap.add_argument("--item", action="append", default=[],
                    help="item_id:variant:qty (variant may be empty), repeatable")
    ap.add_argument("--mandate", help="existing mandate JWT (default: issue one)")
    ap.add_argument("--max-total", type=int, default=500000)
    ap.add_argument("--max-per-txn", type=int, default=300000)
    ap.add_argument("--pay-only", action="store_true", help="pay an existing pending order")
    ap.add_argument("--txn-ref", help="required with --pay-only")
    ap.add_argument("--forged", action="store_true", help="demo: tamper the mandate before paying")
    ap.add_argument("--over-cap", action="store_true",
                    help="demo: pay under a mandate with caps below the charge")
    ap.add_argument("--yes", action="store_true", help="skip the interactive approval prompt")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.shop_url, timeout=15)
    manifest = client.get("/.well-known/agent-commerce.json").json()
    shop_id: str = manifest["merchant"]["id"]
    print(f"shop: {manifest['merchant']['name']} ({shop_id}), pays via "
          f"{manifest['payment']['provider']} [{manifest['payment']['mode']}]")

    token = args.mandate or mandate.issue(args.max_total, args.max_per_txn,
                                          [shop_id], ttl_seconds=3600)
    jti = mandate.verify(token)["jti"]
    print(f"mandate jti {jti}: max_total {_rupees(args.max_total)}, "
          f"max_per_txn {_rupees(args.max_per_txn)}" if not args.mandate else f"mandate jti {jti}")

    if args.pay_only:
        if not args.txn_ref:
            sys.exit("--pay-only requires --txn-ref")
        order: dict[str, Any] = client.get(f"/order/{args.txn_ref}").json()
        if order.get("status") != "pending":
            sys.exit(f"order {args.txn_ref} is not pending: {json.dumps(order, indent=2)}")
    else:
        if not args.item:
            sys.exit("give at least one --item item_id:variant:qty")
        cart_id = client.post("/cart").json()["cart_id"]
        for spec in args.item:
            item_id, variant, qty = spec.split(":")
            r = client.patch(f"/cart/{cart_id}",
                             json={"op": "add", "item_id": item_id, "variant": variant,
                                   "qty": int(qty)})
            if r.status_code != 200:
                body = r.json()
                sys.exit(f"add {spec} failed [{body.get('code')}]: {body.get('why')} "
                         f"(available actions: {body.get('available_actions')})")
            print(f"  added {item_id} {variant or '-'} x{qty}")
        quote = client.post(f"/cart/{cart_id}/coupons").json()
        print(f"coupons: {quote['best']['arithmetic']}")
        if quote.get("near_miss"):
            print(f"near-miss: {quote['near_miss']['math']}")
        r = client.post("/order", json={"cart_id": cart_id},
                        headers={"Authorization": f"Mandate {token}",
                                 "Idempotency-Key": f"order-{uuid.uuid4().hex[:8]}"})
        if r.status_code != 201:
            body = r.json()
            sys.exit(f"order refused [{body.get('code')}]: {body.get('why')}")
        order = r.json()

    charge: int = order["charge_amount"]
    print(f"\norder {order['txn_ref']}: charge {_rupees(charge)} ({charge} paise)")
    for li in order["line_items"]:
        print(f"  {li['item_id']} {li['variant'] or '-'} x{li['qty']} @ {_rupees(li['unit_price_paise'])}")

    if not args.yes:
        answer = input(f"\nApprove THIS basket from {shop_id} at {_rupees(charge)}? [y/N] ")
        if answer.strip().lower() != "y":
            print("not approved; nothing charged")
            sys.exit(1)
    appr = approval.issue(shop_id, order["txn_ref"], order["line_items"], charge, jti)
    print("cart-bound approval signed (5-minute window, single-use nonce)")

    pay_token = token
    if args.forged:
        head, payload, sig = token.split(".")
        pay_token = f"{head}.{payload}.{'A' * len(sig)}"
        print("\n[demo] paying with a FORGED mandate signature:")
    if args.over_cap:
        tiny = mandate.issue(1000, 1000, [shop_id])
        appr = approval.issue(shop_id, order["txn_ref"], order["line_items"], charge,
                              mandate.verify(tiny)["jti"])
        pay_token = tiny
        print(f"\n[demo] paying under a mandate capped at {_rupees(1000)} for a "
              f"{_rupees(charge)} charge:")

    try:
        result = wallet.pay(pay_token, appr, shop_id, charge, order["txn_ref"],
                            shop_url=args.shop_url)
    except errors.VelcrowError as exc:
        _die_with_chains(shop_id, exc)
        return
    print(f"\nwallet: all five checks passed")
    print(f"  razorpay test order: {result['razorpay_order_id']}")
    print(f"  payment ref:         {result['payment_ref']}")

    confirm = client.post("/confirm-payment",
                          json={"txn_ref": order["txn_ref"],
                                "razorpay_order_id": result["razorpay_order_id"],
                                "payment_ref": result["payment_ref"]},
                          headers={"Idempotency-Key": f"confirm-{order['txn_ref']}"}).json()
    print(f"shop confirmed: status={confirm['status']}, charged {_rupees(confirm['charge_amount'])}")
    for actor in ("buyer", shop_id):
        ok, bad = chainlog.verify_chain(actor)
        print(f"chain [{actor}]: {'GREEN' if ok else f'BROKEN at {bad}'}")


if __name__ == "__main__":
    main()
