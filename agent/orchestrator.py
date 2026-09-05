"""The orchestrator: one place that decides WHICH agent gets the work.

VelcrowAI runs a small fleet of agents with different jobs and different
temperaments. This module is the traffic control between them - the thing a
framework would call an orchestration graph, written as plain Python so every
edge is a line you can point at:

    routing     an incoming message is judged (by a model, with a fixed
                answer space and a deterministic fallback) and handed to the
                right agent: the per-shop shopping assistant, or the
                cross-shop buyer flow;
    scheduling  the growth agent and the abandoned-cart sweep are woken on
                clocks, with nobody present;
    handoffs    every time work crosses from one agent to another it is
                chain-logged as an `orchestrator_handoff`, so the audit page
                can show the coordination itself, not just its results.

What the orchestrator deliberately is NOT: a doer. It never calls a shop,
never touches a cart, and can never reach money - it only chooses which
agent does. The wallet's five checks sit downstream of every route it picks.
"""
from __future__ import annotations

from typing import Any, Callable

from common import chainlog

# The fleet, as facts. This is what /orchestrator/status serves and what the
# demo points at when asked "where is the orchestration?".
AGENTS: dict[str, dict[str, str]] = {
    "shopping-assistant": {
        "kind": "LLM tool loop (9 tools)",
        "trigger": "a shopper's sentence - widget or WhatsApp",
        "decides": "which tools to call, in what order, and when to stop",
        "cannot": "pay; see stock numbers; add the same product twice in a turn",
    },
    "buyer-agent": {
        "kind": "deterministic rules + trust-weighted ranking (zero LLM)",
        "trigger": "a goal with a budget, routed here by the message router",
        "decides": "which shops' options fit the rules and how they rank",
        "cannot": "exceed the budget - the stated cap becomes the mandate",
    },
    "growth-agent": {
        "kind": "LLM tool loop (7 tools), simulation-gated",
        "trigger": "scheduler, hourly per shop - nobody watching",
        "decides": "whether the merchant's numbers justify a proposal at all",
        "cannot": "apply anything; propose without a supporting simulation",
    },
    "message-router": {
        "kind": "LLM classifier, fixed answer space, deterministic fallback",
        "trigger": "every WhatsApp text",
        "decides": "instruction-in-a-shop vs cross-shop goal; which shop",
        "cannot": "invent a destination - answers outside the enum are ignored",
    },
    "outreach": {
        "kind": "deterministic code behind a signature-verified webhook",
        "trigger": "restocks, quiet carts, button taps",
        "decides": "nothing - it executes decisions and enforces say-once",
        "cannot": "message anyone twice about the same thing, or skip the wallet",
    },
}


_GOALISH = None  # compiled lazily
def route(text: str, current_key: str | None, shops: dict[str, str],
          choose_shop: Callable[[str, str | None], str]) -> dict[str, str]:
    """Judge one message: {'mode': 'shop'|'goal', 'shop': key}.

    Division of labour, learned the hard way: the MODE (is this an
    instruction, or a goal to satisfy at the best store?) is judgment and
    belongs to the model, with a regex fallback so an outage cannot stall a
    shopper. The SHOP is a vocabulary lookup against the catalogs - lexical
    work the model kept fumbling ("Dupattas" stayed at the grocer) - so it
    belongs to deterministic code that cannot miss a word it contains.
    """
    global _GOALISH
    if _GOALISH is None:
        import re
        _GOALISH = re.compile(
            r"\b(find me|find|cheapest|best price|compare)\b"
            r"|\b(under|below|upto|up to|max)\s*(rs\.?|inr|₹)?\s*\d", re.I)
    try:
        from agent import llm
        mode = llm.route_wa(text, current_key or "grocery", shops)["mode"]
    except Exception:
        mode = "goal" if _GOALISH.search(text) else "shop"
    return {"mode": mode, "shop": choose_shop(text, current_key)}


def handoff(surface: str, to_agent: str, why: str, **data: Any) -> None:
    """Write the coordination down. Every arrow in the orchestration graph
    lands on the chain as its own event, so 'where is the orchestration?'
    has the same answer as everything else here: on the audit page."""
    chainlog.append("buyer", "orchestrator_handoff",
                    f"[{surface}] -> {to_agent}: {why}",
                    {"surface": surface, "to_agent": to_agent, **data})


def register_schedules(sched: Any, installed_shops: dict[str, dict[str, Any]],
                       growth_run: Callable[..., Any],
                       sweep: Callable[..., Any]) -> None:
    """The clock-driven half of the graph: which agents run unattended,
    how often, and under what overlap rules - declared in one place."""
    for installed in installed_shops.values():
        sched.add_job(growth_run, "interval", hours=1, args=[installed],
                      id=f"growth-{installed['shop_id']}", max_instances=1,
                      coalesce=True)
    sched.add_job(sweep, "interval", minutes=10,
                  id="whatsapp-abandoned-sweep", max_instances=1, coalesce=True)


def status(recent: int = 20) -> dict[str, Any]:
    """The fleet and its recent coordination, for the console and the demo."""
    handoffs = [e for e in chainlog.tail("buyer", 200)
                if e["event"] == "orchestrator_handoff"][-recent:]
    return {
        "agents": AGENTS,
        "recent_handoffs": handoffs,
        "note": ("Orchestration here is code you can read: a model routes, a "
                 "scheduler wakes, and every handoff between agents is a "
                 "chain-logged event. No agent can hand work to the wallet - "
                 "only a human's approval can."),
    }
