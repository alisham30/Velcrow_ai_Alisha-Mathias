r"""Conversation regression suite for the agent (spec 0.8, 6.3).

Replays every phrasing that has broken the agent and checks what actually
happened: the resulting cart, which tools were chosen, and — only where the
wording matters — what the reply did or did not say.

This talks to the live services and the live model, so it is NOT part of
pytest (which stays fast and offline). Run it after any change to the prompt,
the tools or the loop.

  .\.venv\Scripts\python.exe -m lab.regress
  .\.venv\Scripts\python.exe -m lab.regress --case money-is-quoted-not-computed -v
  .\.venv\Scripts\python.exe -m lab.regress --repeat 3        # shake out flakiness

Exit code is 0 only if every case passed every repeat.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

AGENT = "http://127.0.0.1:8003"
SHOP_PORTS = {"grocery": 8001, "apparel": 8002}
CASES_FILE = Path(__file__).parent / "conversations.json"
TURN_TIMEOUT = 60.0


class Failure(Exception):
    pass


def _shop_url(shop: str) -> str:
    return f"http://127.0.0.1:{SHOP_PORTS[shop]}"


def _run_turn(shop: str, cart_id: str, message: str, history: list[dict[str, str]],
              token: str | None) -> tuple[dict[str, Any], str | None]:
    started = httpx.post(f"{AGENT}/agent/chat", timeout=25, json={
        "shop": shop, "cart_id": cart_id, "message": message,
        "history": history[-6:], "mandate_token": token}).json()
    run_id = started["run_id"]
    token = started.get("mandate_token", token)

    deadline = time.time() + TURN_TIMEOUT
    run: dict[str, Any] = {}
    while time.time() < deadline:
        run = httpx.get(f"{AGENT}/agent/run/{run_id}", timeout=20).json()
        if run["done"]:
            break
        time.sleep(0.4)
    else:
        raise Failure(f"turn timed out after {TURN_TIMEOUT}s: {message!r}")
    return run, token


def _check_turn(turn: dict[str, Any], events: list[dict[str, Any]], reply: str) -> list[str]:
    expect = turn.get("expect") or {}
    problems: list[str] = []
    called = [e["tool"] for e in events if e["kind"] == "tool" and e["ok"]]
    lowered = reply.lower()

    for tool in expect.get("must_call", []):
        if tool not in called:
            problems.append(f"expected a successful {tool}, tools were {called or 'none'}")
    for tool in expect.get("must_not_call", []):
        if tool in called:
            problems.append(f"{tool} was called but must not have been")
    for needle in expect.get("reply_contains", []):
        if needle.lower() not in lowered:
            problems.append(f"reply is missing {needle!r}")
    for needle in expect.get("reply_excludes", []):
        if needle.lower() in lowered:
            problems.append(f"reply contains {needle!r}, which it must not")
    return problems


def _check_cart(shop: str, cart_id: str, wanted: list[dict[str, Any]]) -> list[str]:
    cart = httpx.get(f"{_shop_url(shop)}/cart/{cart_id}", timeout=20).json()
    actual = sorted(
        ((l["item_id"], l["variant"], l["qty"]) for l in cart["items"]),
        key=lambda t: (t[0], t[1]),
    )
    expected = sorted(
        ((w["item_id"], w.get("variant", ""), w["qty"]) for w in wanted),
        key=lambda t: (t[0], t[1]),
    )
    if actual != expected:
        return [f"cart is {actual or 'empty'}, expected {expected or 'empty'}"]
    return []


def run_case(case: dict[str, Any], verbose: bool) -> list[str]:
    shop = case["shop"]
    cart_id = httpx.post(f"{_shop_url(shop)}/cart", json={}, timeout=20).json()["cart_id"]
    history: list[dict[str, str]] = []
    token: str | None = None
    problems: list[str] = []

    for turn in case["turns"]:
        run, token = _run_turn(shop, cart_id, turn["say"], history, token)
        events = run["events"]
        reply = next((e["text"] for e in events if e["kind"] == "message"), "")
        if any(e["kind"] == "degraded" for e in events):
            problems.append("the model was unreachable; this run proves nothing")
        if verbose:
            print(f"    > {turn['say']}")
            for e in events:
                if e["kind"] == "tool":
                    mark = "ok" if e["ok"] else "REFUSED"
                    print(f"        [{mark}] {e['tool']} -> {e['result_summary']}")
            print(f"      {reply}")
        problems += [f"turn {turn['say']!r}: {p}" for p in _check_turn(turn, events, reply)]
        history.append({"role": "user", "content": turn["say"]})
        history.append({"role": "assistant", "content": reply})

    if "cart" in case:
        problems += _check_cart(shop, cart_id, case["cart"])
    return problems


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Replay conversations that have broken the agent")
    ap.add_argument("--case", action="append", help="run only these cases (repeatable)")
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every turn and tool call")
    args = ap.parse_args()

    try:
        httpx.get(f"{AGENT}/health", timeout=10)
        for shop in SHOP_PORTS:
            httpx.get(f"{_shop_url(shop)}/catalog", timeout=10)
    except Exception as exc:
        sys.exit(f"services are not all up ({exc}). Start :8001, :8002 and :8003 first.")

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cases}
        if missing:
            sys.exit(f"no such case(s): {sorted(missing)}")

    failures: list[tuple[str, int, list[str]]] = []
    started = time.time()
    for case in cases:
        for attempt in range(1, args.repeat + 1):
            label = case["name"] + (f" [{attempt}/{args.repeat}]" if args.repeat > 1 else "")
            print(f"  {label} ... ", end="", flush=True)
            if args.verbose:
                print()
            try:
                problems = run_case(case, args.verbose)
            except Exception as exc:
                problems = [f"{type(exc).__name__}: {exc}"]
            if problems:
                print("FAIL")
                for p in problems:
                    print(f"      - {p}")
                print(f"      (regression guard: {case['why']})")
                failures.append((case["name"], attempt, problems))
            else:
                print("pass")

    total = len(cases) * args.repeat
    elapsed = int(time.time() - started)
    print(f"\n{total - len(failures)}/{total} passed in {elapsed}s")
    if failures:
        print("failed: " + ", ".join(sorted({name for name, _, _ in failures})))
        sys.exit(1)


if __name__ == "__main__":
    main()
