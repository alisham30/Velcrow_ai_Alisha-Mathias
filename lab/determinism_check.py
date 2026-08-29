r"""The 'not a script' proof (spec 16.13): same sentence, two worlds.

A script fed the same input does the same thing twice. An agent reads the
world first. This sends ONE sentence to the live widget twice - once with the
item on the shelf, once after real orders have emptied that shelf - and prints
the two tool traces side by side. Same words, same code, same prompt; the only
thing that changed is the stock, and the agent's chosen actions and answer
change with it.

Nothing is mocked. The shelf is emptied by genuinely buying the stock through
the same endpoints any shopper uses, and both traces come back from the same
running service. Run it, then read the two traces aloud - the difference IS
the demo:

    .\.venv\Scripts\python.exe -m lab.determinism_check
"""
from __future__ import annotations

import time
import uuid

import httpx

from lab import console

AGENT = "http://127.0.0.1:8003"
SHOP = "http://127.0.0.1:8001"
ITEM, SENTENCE = "filter-coffee-250g", "add 2 packs of filter coffee"


def one_turn(http: httpx.Client, message: str) -> dict:
    """One real widget turn: fresh cart, same sentence, wait for the reply."""
    cart = http.post(f"{SHOP}/cart", json={}).json()["cart_id"]
    run = http.post(f"{AGENT}/agent/chat",
                    json={"shop": "grocery", "cart_id": cart, "message": message}).json()
    for _ in range(45):
        time.sleep(1)
        d = http.get(f"{AGENT}/agent/run/{run['run_id']}").json()
        if d.get("done"):
            return d
    raise TimeoutError("the agent did not finish a turn in 45s")


def trace_of(turn: dict) -> list[tuple[str, bool]]:
    return [(e.get("call_display") or e.get("tool", "?"), bool(e.get("ok")))
            for e in turn["events"] if e.get("kind") == "tool"]


def reply_of(turn: dict) -> str:
    return next((e["text"] for e in turn["events"] if e.get("kind") == "message"), "(none)")


def stock_of(http: httpx.Client) -> int:
    return int(http.get(f"{SHOP}/product/{ITEM}").json().get("stock", 0))


def buy_the_shelf(http: httpx.Client, stock: int) -> None:
    """Empty the shelf the honest way: a real mandated order for all of it."""
    token = http.post(f"{AGENT}/mandate",
                      json={"shops": ["freshkart"], "max_total_paise": 5_000_000,
                            "max_per_txn_paise": 5_000_000}).json()["token"]
    auth = {"Authorization": f"Mandate {token}"}
    cart = http.post(f"{SHOP}/cart", json={}).json()["cart_id"]
    http.post(f"{SHOP}/cart/{cart}/fulfil", headers=auth,
              json={"item_id": ITEM, "variant": "", "qty": stock, "mode": "add"})
    placed = http.post(f"{SHOP}/order", headers={**auth,
                                                 "Idempotency-Key": f"det-{uuid.uuid4().hex[:8]}"},
                       json={"cart_id": cart}).json()
    http.post(f"{SHOP}/confirm-payment",
              json={"txn_ref": placed["txn_ref"],
                    "razorpay_order_id": f"order_det_{uuid.uuid4().hex[:8]}",
                    "payment_ref": "pay_det"})


def show(out, label: str, stock: int, turn: dict) -> list[tuple[str, bool]]:
    steps = trace_of(turn)
    print(f"\n[{label}]  shelf: {stock} in stock", file=out)
    for i, (call, ok) in enumerate(steps, 1):
        print(f"   {i}. {'ok ' if ok else 'ERR'}  {call}", file=out)
    print(f"   -> {reply_of(turn)[:220]}", file=out)
    return steps


def main() -> int:
    out = console()
    try:
        httpx.get(f"{AGENT}/health", timeout=5)
    except Exception:
        print("The VelcrowAI service is not answering on :8003. Start everything with "
              ".\\run_all.ps1 first.", file=out)
        return 1

    with httpx.Client(timeout=90) as http:
        stock = stock_of(http)
        if stock <= 0:
            http.post(f"{SHOP}/admin/restock",
                      json={"item_id": ITEM, "variant": "", "qty": 8})
            stock = stock_of(http)

        print(f'\nSame sentence, twice: "{SENTENCE}"', file=out)
        a_steps = show(out, "world A", stock, one_turn(http, SENTENCE))

        buy_the_shelf(http, stock)
        empty = stock_of(http)
        b_steps = show(out, "world B", empty, one_turn(http, SENTENCE))

    a_calls = [c for c, _ in a_steps]
    b_calls = [c for c, _ in b_steps]
    same = a_calls == b_calls and all(ok for _, ok in a_steps) == all(ok for _, ok in b_steps)
    print("\n" + ("SAME trace twice - that would be a script, and it is a FAILURE of this "
                  "check." if same else
                  "The traces differ because the WORLD differed - the model read the shelf "
                  "and chose\ndifferent actions. A script cannot do that, and nothing above "
                  "was mocked."), file=out)
    print(f"   world A: {len(a_steps)} step(s), all ok: {all(ok for _, ok in a_steps)}",
          file=out)
    print(f"   world B: {len(b_steps)} step(s), all ok: {all(ok for _, ok in b_steps)}\n",
          file=out)
    return 1 if same else 0


if __name__ == "__main__":
    raise SystemExit(main())
