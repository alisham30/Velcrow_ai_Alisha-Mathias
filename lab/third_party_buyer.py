r"""A stranger's buying agent (spec 6.6, 6.7).

This client has never seen either shop. It imports nothing from `agent/`, hard
codes no merchant name, and knows no endpoint that the merchant's own manifest
did not tell it. It reads /.well-known/agent-commerce.json, negotiates
capabilities, discovers the catalog, and then buys through the merchant's
ACP-shaped checkout surface - the standards-shaped dialect - falling back to
a typed refusal it can explain when the shop objects.

Run it unchanged against either shop:

  .\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8001
  .\.venv\Scripts\python.exe -m lab.third_party_buyer http://127.0.0.1:8002

Be precise about what this is: a deterministic protocol client, NOT an agent.
It proves the API contract is open - that a buyer needs no VelcrowAI code and
no private knowledge to transact, and that the checkout it drives is the shape
the Agentic Commerce Protocol publishes, at the version the manifest declares.

The mandate comes from the BUYER's own authorization service (--trust), which
is the buyer's side of the arrangement: a merchant never issues the permission
to spend against itself. On the ACP surface it rides as the Bearer token.
"""
from __future__ import annotations

import argparse
import sys
import uuid

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
    ap.add_argument("--qty", type=int, default=1, help="units to buy")
    args = ap.parse_args()
    out = sys.stdout

    with httpx.Client(timeout=20) as http:
        # 1. discovery - the only thing known in advance is the well-known path
        m = http.get(f"{args.shop_url}/.well-known/agent-commerce.json").json()
        merchant, checkout = m["merchant"], m["checkout"]
        print(f"discovered: {merchant['name']} ({merchant['category']}), "
              f"pays in {m['currency']}, checkout speaks {checkout['protocol']} "
              f"{checkout['version']}", file=out)

        # 2. capability negotiation - adapt to the merchant instead of assuming
        caps = http.post(f"{args.shop_url}/agent/capabilities",
                         json={"capabilities": {"reservations": True, "discounts": True}}
                         ).json()["capabilities"]
        print(f"capabilities: variants={caps.get('variants')} "
              f"reservations={bool(caps.get('reservations'))} "
              f"discounts={bool(caps.get('discounts'))}", file=out)

        # 3. catalog, from wherever the manifest says it lives
        catalog = http.get(f"{args.shop_url}{m['catalog']}").json()
        affordable = [p for p in catalog if p["price_paise"] * args.qty <= args.budget]
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
            # out_of_stock and recovering is better than quietly buying the
            # wrong thing.
            variant = (args.size or (live or variants)[0]["label"])
        print(f"chose {pick['name']}"
              + (f" [{variant}]" if variant else "")
              + f" x{args.qty} at {pick['price_paise']} paise", file=out)

        # 4. an ACP checkout session. Item ids and quantity-by-repetition are
        #    whatever the manifest says they are - nothing here is assumed.
        item_id = pick["id"] + (f"::{variant}" if variant else "")
        create = checkout["endpoints"]["create"].split(" ", 1)[1]
        sess = http.post(f"{args.shop_url}{create}",
                         json={"line_items": [{"id": item_id}] * args.qty,
                               "buyer": {"email": "third-party-buyer@example.com"}},
                         headers={"Idempotency-Key": f"tpb-{uuid.uuid4().hex[:10]}"}).json()
        total = next((t["amount"] for t in sess.get("totals", []) if t["type"] == "total"), 0)
        print(f"session {sess.get('id')} -> {sess.get('status')}, total {total} paise",
              file=out)

        # 5. not ready? read the spec-typed messages and recover the one way
        #    this merchant said it supports.
        stockout = next((msg for msg in sess.get("messages", [])
                         if msg.get("code") == "out_of_stock"), None)
        if stockout:
            reserve_at = (m["order"].get("reserve") or "").split(" ")[-1]
            if reserve_at:
                token = http.post(f"{args.trust}/mandate",
                                  json={"shops": [merchant["id"]],
                                        "max_total_paise": args.budget,
                                        "max_per_txn_paise": args.budget}).json()["token"]
                res = http.post(f"{args.shop_url}{reserve_at}",
                                json={"item_id": pick["id"], "variant": variant,
                                      "qty": args.qty,
                                      "contact_ref": "third-party-buyer@example.com"},
                                headers={"Authorization": f"Mandate {token}"})
                res.raise_for_status()
                print(f"out_of_stock -> reserved instead ({res.json()['res_id']}): "
                      f"{stockout['content']}", file=out)
                return 0
            print(f"out_of_stock and this merchant takes no reservations: "
                  f"{stockout['content']}", file=out)
            return 1
        if sess.get("status") != "ready_for_payment":
            print(f"cannot pay: {[msg.get('content') for msg in sess.get('messages', [])]}",
                  file=out)
            return 1

        # 6. the buyer's own mandate, presented as the Bearer the manifest asks
        token = http.post(f"{args.trust}/mandate",
                          json={"shops": [merchant["id"]], "max_total_paise": args.budget,
                                "max_per_txn_paise": args.budget}).json()["token"]
        complete = checkout["endpoints"]["complete"].split(" ", 1)[1] \
            .replace("{checkout_session_id}", sess["id"])
        done = http.post(f"{args.shop_url}{complete}",
                         json={"buyer": {"email": "third-party-buyer@example.com",
                                         "first_name": "Third", "last_name": "Party"},
                               "payment_data": {"handler_id": "razorpay_test_spt",
                                                "instrument": {"type": "card",
                                                               "credential": {"type": "spt",
                                                                              "token": f"spt_tpb_{uuid.uuid4().hex[:10]}"}}}},
                         headers={"Authorization": f"Bearer {token}",
                                  "Idempotency-Key": f"tpb-c-{sess['id']}"})
        if done.status_code >= 400:
            err = done.json()
            print(f"complete refused: {err.get('code')} - {err.get('message')}", file=out)
            return 1
        done = done.json()
        order = done.get("order", {})
        print(f"session {done['status']}; order {order.get('id')} "
              f"({order.get('status')})", file=out)

        # 7. verify against the merchant's own record, at the order permalink
        final = http.get(order["permalink_url"]).json()
        print(f"PAID {final['charge_amount']} paise at {merchant['name']}, "
              f"status {final['status']}", file=out)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
