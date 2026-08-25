r"""Replay a multi-turn widget conversation from the terminal, the way the
browser does it (history is carried forward). Phase 4 harness.

  .\.venv\Scripts\python.exe -m lab.convo grocery "1 kg basmati rice" "okayy add"
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import httpx
from dotenv import load_dotenv

AGENT = "http://127.0.0.1:8003"
SHOP_PORTS = {"grocery": 8001, "apparel": 8002}


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Replay a widget conversation")
    ap.add_argument("shop", choices=sorted(SHOP_PORTS))
    ap.add_argument("messages", nargs="+")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    shop_url = f"http://127.0.0.1:{SHOP_PORTS[args.shop]}"
    cart_id = httpx.post(f"{shop_url}/cart", json={}, timeout=15).json()["cart_id"]
    history: list[dict[str, str]] = []
    token: str | None = None

    for message in args.messages:
        print(f"\n> {message}")
        started = httpx.post(f"{AGENT}/agent/chat", timeout=20, json={
            "shop": args.shop, "cart_id": cart_id, "message": message,
            "history": history[-6:], "mandate_token": token}).json()
        token = started.get("mandate_token", token)
        run_id = started["run_id"]

        deadline = time.time() + args.timeout
        events: list[dict[str, Any]] = []
        while time.time() < deadline:
            run = httpx.get(f"{AGENT}/agent/run/{run_id}", timeout=15).json()
            events = run["events"]
            if run["done"]:
                break
            time.sleep(0.5)

        reply = ""
        for e in events:
            if e["kind"] == "tool":
                print(f"    [{e['tool']}] {e['result_summary']}")
            elif e["kind"] == "message":
                reply = e["text"]
            elif e["kind"] == "degraded":
                print("    [degraded] model unreachable, deterministic fallback")
        print(f"  {reply}")
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})

    cart = httpx.get(f"{shop_url}/cart/{cart_id}", timeout=15).json()
    print("\nCART:")
    for line in cart["items"]:
        label = f"{line['name']}{' ' + line['variant'] if line['variant'] else ''}"
        print(f"  {label} x{line['qty']}")
    if not cart["items"]:
        print("  (empty)")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
