"""The agent loop (spec 0.8, 6.3) and the run registry behind SSE.

The loop is: gather live context -> hand it to the model with the tool
definitions -> execute whatever tool the model returns -> feed the result
back -> repeat until the model answers instead of calling a tool. The number
of rounds is whatever the model needs; nothing here sequences the tools.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from agent import llm, tools
from common import chainlog

RUNS: dict[str, "Run"] = {}
MAX_RUNS_KEPT = 50

# Conversation state lives here, keyed by the cart the shopper is working on.
# It holds the real message list — including the assistant's tool calls and the
# tool results — so the agent can refer back to a product id it already looked
# up instead of inventing one on the next turn.
CONVERSATIONS: dict[tuple[str, str], list[dict[str, Any]]] = {}
MAX_TURNS_KEPT = 6


def _trim(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop whole turns from the front, never splitting an assistant's
    tool_calls from the tool results that answer them."""
    starts = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(starts) <= MAX_TURNS_KEPT:
        return messages
    return messages[starts[-MAX_TURNS_KEPT]:]


class Run:
    """One turn of the conversation: its event log plus a live subscriber queue."""

    def __init__(self, run_id: str, shop_id: str) -> None:
        self.run_id = run_id
        self.shop_id = shop_id
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.queues: list[asyncio.Queue] = []

    def emit(self, kind: str, **data: Any) -> None:
        event = {"seq": len(self.events), "kind": kind, "ts": time.time(), **data}
        self.events.append(event)
        for q in self.queues:
            q.put_nowait(event)

    def finish(self) -> None:
        self.done = True
        for q in self.queues:
            q.put_nowait(None)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for event in self.events:  # replay so a late subscriber sees the whole turn
            q.put_nowait(event)
        if self.done:
            q.put_nowait(None)
        else:
            self.queues.append(q)
        return q


def new_run(shop_id: str) -> Run:
    run = Run("run_" + uuid.uuid4().hex[:12], shop_id)
    RUNS[run.run_id] = run
    for stale in list(RUNS)[:-MAX_RUNS_KEPT]:
        RUNS.pop(stale, None)
    return run


def gather_context(shop: dict[str, Any], cart_id: str, mandate_claims: dict[str, Any]) -> dict[str, Any]:
    """Live context for this turn — read fresh from the shop every time, so
    two runs of the same sentence differ when stock or price differs."""
    with httpx.Client(base_url=shop["url"], timeout=15) as c:
        catalog = c.get("/catalog").json()
        cart = c.get(f"/cart/{cart_id}").json()
        caps = c.post("/agent/capabilities", json={"capabilities": {}}).json()["capabilities"]
    return {
        "shop_id": shop["shop_id"],
        "shop_name": shop["name"],
        "shop_category": shop["category"],
        "categories": sorted({p["category"] for p in catalog}),
        "catalog_size": len(catalog),
        "variant_kind": caps.get("variants", "variant"),
        "cart_lines": cart["items"],
        "cart_count": sum(l["qty"] for l in cart["items"]),
        "subtotal_paise": cart["subtotal_paise"],
        "max_per_txn_paise": mandate_claims["max_per_txn"],
        "max_total_paise": mandate_claims["max_total"],
    }


async def run_turn(run: Run, shop: dict[str, Any], cart_id: str, user_text: str,
                   history: list[dict[str, str]], mandate_claims: dict[str, Any]) -> None:
    """Execute one shopper turn end to end, emitting events as it goes."""
    ctx = {"shop_url": shop["url"], "shop_id": shop["shop_id"], "cart_id": cart_id}
    loop = asyncio.get_running_loop()

    try:
        context = await loop.run_in_executor(None, gather_context, shop, cart_id, mandate_claims)
    except Exception as exc:
        run.emit("error", message=f"Could not read the shop: {exc}")
        run.finish()
        return

    run.emit("context", cart_count=context["cart_count"], subtotal_paise=context["subtotal_paise"],
             catalog_size=context["catalog_size"])

    # The system prompt is rebuilt every turn so the context is live, but the
    # conversation underneath it is the real one, tool calls and all.
    key = (shop["shop_id"], cart_id)
    prior = CONVERSATIONS.get(key)
    if prior is None:
        prior = [{"role": t["role"], "content": t["content"]} for t in (history or [])[-6:]]
    messages: list[dict[str, Any]] = [{"role": "system", "content": llm.system_prompt(context)}]
    messages.extend(prior)
    messages.append({"role": "user", "content": user_text})

    degraded = False
    fallback_qty = 1
    cart_touched = False
    # Policy in code, not in the prompt (spec 7.9): one add per product per
    # turn. A model that re-reads its own question, or treats "did you add it?"
    # as an instruction, cannot silently double the basket.
    added_this_turn: set[tuple[str, str]] = set()

    for round_no in range(llm.MAX_ROUNDS):
        try:
            step = await loop.run_in_executor(None, llm.plan, messages)
        except llm.LLMUnavailable as exc:
            # spec 3: a dead API degrades, it does not kill the demo
            degraded = True
            run.emit("degraded", why=str(exc))
            chainlog.append("buyer", "llm_unavailable",
                            f"model unreachable ({exc}); falling back to the deterministic planner",
                            {"shop_id": shop["shop_id"], "run_id": run.run_id})
            step = llm.deterministic_fallback(user_text)
            fallback_qty = step.get("fallback_qty", 1)

        if not step["tool_calls"]:
            final = step["content"] or "I could not work out what to do there."
            messages.append({"role": "assistant", "content": final})
            run.emit("message", text=final, degraded=degraded, rounds=round_no)
            break

        messages.append({
            "role": "assistant",
            "content": step["content"] or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                for tc in step["tool_calls"]
            ],
        })

        for tc in step["tool_calls"]:
            name, args = tc["name"], dict(tc["args"])
            why = args.pop("reason", "") or "no reason given"
            started = time.perf_counter()
            fn = tools.REGISTRY.get(name)
            try:
                if fn is None:
                    raise tools.ToolError(f"no such tool '{name}'", "UNKNOWN_TOOL")
                if name == "add_to_cart":
                    key = (str(args.get("item_id", "")), str(args.get("variant") or ""))
                    if key in added_this_turn:
                        raise tools.ToolError(
                            f"'{key[0]}' was already added in this turn; if the shopper wants a "
                            "different quantity call update_qty on that line instead",
                            "DUPLICATE_ADD")
                result = await loop.run_in_executor(None, lambda: fn(ctx, **args))
                if name == "add_to_cart":
                    added_this_turn.add(key)
                summary = tools.summarise(name, result)
                ok = True
            except tools.ToolError as exc:
                result = {"error": exc.message, "code": exc.code}
                summary = f"refused: {exc.message}"
                ok = False
            except Exception as exc:
                result = {"error": str(exc), "code": "TOOL_CRASH"}
                summary = f"failed: {exc}"
                ok = False
            latency_ms = int((time.perf_counter() - started) * 1000)

            run.emit("tool", tool=name, args=args, why=why, result_summary=summary,
                     latency_ms=latency_ms, ok=ok, round=round_no)
            chainlog.append("buyer", "agent_tool_call",
                            f"{name}({json.dumps(args, ensure_ascii=False)}) -> {summary}; "
                            f"model's reason: {why}",
                            {"run_id": run.run_id, "shop_id": shop["shop_id"], "tool": name,
                             "args": args, "ok": ok, "latency_ms": latency_ms,
                             "degraded": degraded})
            if ok and name in tools.MUTATING:
                cart_touched = True

            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(result, ensure_ascii=False)})

        if degraded:
            # the fallback planner has no second thought: finish the job in code
            text = await loop.run_in_executor(
                None, lambda: _finish_degraded(ctx, messages, fallback_qty))
            messages.append({"role": "assistant", "content": text})
            run.emit("message", text=text, degraded=True, rounds=round_no)
            cart_touched = True
            break
    else:
        stalled = "I stopped after several steps without finishing. Could you narrow that down?"
        messages.append({"role": "assistant", "content": stalled})
        run.emit("message", text=stalled, degraded=degraded, rounds=llm.MAX_ROUNDS)

    CONVERSATIONS[key] = _trim(messages[1:])  # keep everything but the system prompt
    run.emit("cart_changed", changed=cart_touched)
    run.finish()


def _finish_degraded(ctx: dict[str, Any], messages: list[dict[str, Any]], qty: int) -> str:
    """Offline path only: add the best search hit, then report plainly."""
    last = json.loads(messages[-1]["content"])
    matches = last.get("matches") or []
    if not matches:
        return ("The assistant is offline and I found nothing matching that here. "
                "Try naming the item exactly as the shop lists it.")
    best = matches[0]
    variant = ""
    if best.get("variants"):
        available = [v for v in best["variants"] if v["stock"] > 0]
        if not available:
            return f"The assistant is offline. {best['name']} is out of stock in every option."
        variant = available[0]["label"]
    try:
        summary = tools.add_to_cart(ctx, item_id=best["item_id"], qty=qty, variant=variant)
    except tools.ToolError as exc:
        return f"The assistant is offline and the shop refused that: {exc.message}"
    return (f"Offline mode: added {qty} x {best['name']}"
            + (f" ({variant})" if variant else "")
            + f". Cart is now {summary['item_count']} item(s), "
              f"Rs {summary['subtotal_paise'] // 100}.{summary['subtotal_paise'] % 100:02d}.")
