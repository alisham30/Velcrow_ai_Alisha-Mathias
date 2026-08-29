"""The autonomous merchant agent (spec 7.5).

Everything else in this project needs a shopper present. This does not, and
that is the whole point of it. A scheduled job wakes per shop, gets a standing
goal and a toolset, and runs the same loop as the shopper agent:

    Goal: find economically justified opportunities to increase this
          merchant's revenue without violating margin policy. Propose only
          what the numbers support.

Two things make it an agent rather than a cron job wearing a costume:

1. It decides which questions to ask. Nothing here sequences the tools; the
   model picks what to look at, and different data produces different reads.
2. PRODUCING NO PROPOSAL IS A CORRECT OUTCOME. An agent that always finds
   something is a script with a thesaurus. A run that looks at healthy stock
   and tight margins and concludes there is nothing worth doing is logged as
   `no_action` with its reasoning, and that is a success.

The `simulate_*` tools are what separate reasoning from reporting: the agent
can test an idea against projected margin BEFORE proposing it, and discard
ideas that do not pay for themselves. Discarded ideas are recorded too.

Governance is absolute (spec 7.5). Nothing here changes stock or pricing. It
writes proposals; a human approves or rejects them in the console; approval is
what applies the change. Same gate philosophy as the wallet, merchant side.
"""
from __future__ import annotations

import json
import random
import time
import uuid
from typing import Any

import httpx

from agent import llm
from common import bandit, chainlog, money

MAX_ROUNDS = 8
FLOOR_MARGIN_PCT = 12.0   # never propose a discount that leaves less than this

RUNS: dict[str, dict[str, Any]] = {}
MAX_RUNS_KEPT = 30


# -- tools ------------------------------------------------------------------

def _get(shop_url: str, path: str, **params: Any) -> dict[str, Any]:
    with httpx.Client(base_url=shop_url, timeout=20) as c:
        resp = c.get(path, params=params or None)
    resp.raise_for_status()
    return resp.json()


def get_sales_metrics(ctx: dict[str, Any], period_days: float = 7.0, **_: Any) -> dict[str, Any]:
    m = _get(ctx["shop_url"], "/merchant/metrics", days=period_days)
    return {
        "period_days": m["days"], "orders": m["orders"],
        "revenue_display": money.rupees(m["revenue_paise"]),
        "aov_display": money.rupees(m["aov_paise"]),
        "items": [
            {"item_id": r["item_id"], "name": r["name"], "units_sold": r["units_sold"],
             "revenue_display": money.rupees(r["revenue_paise"]), "stock": r["stock"],
             "days_of_stock": r["days_of_stock"]}
            for r in m["items"]
        ],
        "note": ("days_of_stock is null where nothing sold at all - that is a different "
                 "problem from selling slowly, and usually not a discount problem."),
    }


def get_demand_ledger(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Refused demand, with what is STILL being lost separated from what has
    already been dealt with. Reasoning from the historical total made the agent
    propose restocking things that had been restocked days ago."""
    d = _get(ctx["shop_url"], "/merchant/demand-ledger")
    return {
        "outstanding_display": money.rupees(d["outstanding_value_paise"]),
        "recovered_display": money.rupees(d["recovered_value_paise"]),
        "told_display": money.rupees(d.get("told_value_paise", 0)),
        "lapsed_display": money.rupees(d["lapsed_value_paise"]),
        "rows": [
            {"item_id": r["item_id"], "variant": r["variant"],
             "outstanding_units": r["outstanding_units"],
             "outstanding_display": money.rupees(r["outstanding_value_paise"]),
             "state": r["state"], "action": r.get("action", "restock"),
             "in_stock": r["in_stock"],
             "restock_date": r["restock_date"],
             "waiting": len(r.get("reservations", []))}
            for r in d["rows"]
        ],
        "note": ("Act on OUTSTANDING only. Recovered means the shopper came back and paid - "
                 "that money is already in the till. Told means restocked and the shopper "
                 "informed, waiting on them. Lapsed means restocked with nobody to tell. None "
                 "of those three is money you are still losing, and proposing a restock for "
                 "them wastes the merchant's cash. Check each row's action: 'restock' needs "
                 "stock bought, 'notify' means the stock is already on the shelf and the "
                 "shopper simply has not been told - proposing to buy more there is money "
                 "spent on a problem that does not exist."),
    }


def get_inventory(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    d = _get(ctx["shop_url"], "/merchant/inventory")
    return {"items": [
        {"item_id": r["item_id"], "name": r["name"], "variant": r["variant"],
         "stock": r["stock"], "restock_date": r["restock_date"]}
        for r in d["items"]]}


def get_margins(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    d = _get(ctx["shop_url"], "/merchant/margins")
    return {
        "floor_margin_pct": FLOOR_MARGIN_PCT,
        "items": [
            {"item_id": r["item_id"], "name": r["name"],
             "price_display": money.rupees(r["price_paise"]),
             "margin_pct": r["margin_pct"],
             "max_discount_pct": max(0.0, round(r["margin_pct"] - FLOOR_MARGIN_PCT, 1))}
            for r in d["items"]
        ],
        "note": ("max_discount_pct already reserves the floor margin. A discount above it "
                 "sells at or below the policy floor and must not be proposed."),
    }


def simulate_discount(ctx: dict[str, Any], item_id: str, pct: float, days: float = 7.0,
                      **_: Any) -> dict[str, Any]:
    """Project what a discount does to margin before anyone proposes it.

    Uses this shop's OWN recent sales as the baseline and a stated, visible
    uplift assumption - not a forecast dressed up as a fact. The verdict is
    computed here so the model cannot talk itself past the margin floor.
    """
    metrics = _get(ctx["shop_url"], "/merchant/metrics", days=days)
    margins = {r["item_id"]: r for r in _get(ctx["shop_url"], "/merchant/margins")["items"]}
    row = next((r for r in metrics["items"] if r["item_id"] == item_id), None)
    m = margins.get(item_id)
    if row is None or m is None:
        return {"error": f"no such item '{item_id}'"}

    pct = float(pct)
    headroom = max(0.0, m["margin_pct"] - FLOOR_MARGIN_PCT)
    price = m["price_paise"]
    discounted = int(price * (1 - pct / 100))
    unit_margin_before = price - m["cost_paise"]
    unit_margin_after = discounted - m["cost_paise"]

    baseline_units = row["units_sold"]
    # A stated elasticity, not a measured one. Named so nobody mistakes it for
    # data: roughly 1.5% more units per 1% off, which is optimistic and should
    # be read as a best case.
    uplift = 1 + (pct * 1.5) / 100
    projected_units = round(baseline_units * uplift, 1)

    before = baseline_units * unit_margin_before
    after = projected_units * unit_margin_after
    return {
        "item_id": item_id, "name": m["name"], "pct": pct, "days": days,
        "margin_pct_now": m["margin_pct"],
        "margin_pct_after": round((discounted - m["cost_paise"]) / discounted * 100, 1)
        if discounted else 0.0,
        "max_safe_pct": headroom,
        "within_policy": pct <= headroom,
        "baseline_units": baseline_units, "projected_units": projected_units,
        "margin_before_display": money.rupees(int(before)),
        "margin_after_display": money.rupees(int(after)),
        "margin_delta_display": money.rupees(int(after - before)),
        "pays_for_itself": after > before,
        "assumption": "projected units assume ~1.5% more sold per 1% off - a best case, not data",
        "verdict": (
            f"refuse: {pct}% breaches the {FLOOR_MARGIN_PCT}% margin floor "
            f"(max safe is {headroom}%)" if pct > headroom else
            f"viable: projected margin {'up' if after > before else 'down'} "
            f"{money.rupees(int(abs(after - before)))} over {days} days"),
    }


def simulate_restock(ctx: dict[str, Any], item_id: str, qty: int, variant: str = "",
                     **_: Any) -> dict[str, Any]:
    """What restocking would recover, from demand actually refused."""
    ledger = _get(ctx["shop_url"], "/merchant/demand-ledger")
    margins = {r["item_id"]: r for r in _get(ctx["shop_url"], "/merchant/margins")["items"]}
    m = margins.get(item_id)
    if m is None:
        return {"error": f"no such item '{item_id}'"}
    rows = [r for r in ledger["rows"] if r["item_id"] == item_id
            and (not variant or r["variant"] == variant)]
    # Only demand still going unserved. Counting refusals already restocked and
    # acted on would recommend buying stock the merchant already bought.
    lost_units = sum(r["outstanding_units"] for r in rows)
    waiting = sum(len(r.get("reservations", [])) for r in rows)
    # Stock the merchant has ALREADY bought serves this demand first. Without
    # this the simulation told the agent to buy three more sarees while four
    # sat on the shelf; the shopper had been refused before the shelf was
    # refilled and nobody had gone back to tell them. That is a message, not
    # an inventory order, and the two cost very different amounts of money.
    on_hand = sum(r["in_stock"] for r in rows)
    served_by_shelf = min(on_hand, lost_units)
    still_short = lost_units - served_by_shelf

    qty = int(qty)
    recoverable = min(qty, still_short)
    revenue = recoverable * m["price_paise"]
    margin = recoverable * (m["price_paise"] - m["cost_paise"])
    shelf_revenue = served_by_shelf * m["price_paise"]
    return {
        "item_id": item_id, "name": m["name"], "variant": variant, "qty": qty,
        "demand_refused": lost_units, "shoppers_waiting": waiting,
        "already_on_shelf": on_hand,
        "recoverable_by_telling_them": served_by_shelf,
        "recoverable_by_telling_them_display": money.rupees(shelf_revenue),
        "recoverable_units": recoverable,
        "revenue_display": money.rupees(revenue),
        "margin_display": money.rupees(margin),
        "worth_doing": recoverable > 0,
        "verdict": (
            f"{recoverable} of the {qty} would go to demand already refused, "
            f"worth {money.rupees(revenue)}"
            if recoverable else
            f"buy nothing: {served_by_shelf} unit(s) of that refused demand, worth "
            f"{money.rupees(shelf_revenue)}, are ALREADY on the shelf and the shopper has "
            "simply not been told. Restocking spends cash on a problem that does not "
            "exist; the fix is a restock notification"
            if served_by_shelf else
            "nothing was refused for this item, so a restock recovers no lost sale"),
    }


# Which arms are only allowed to reach a merchant on the back of a simulation,
# and which tool has to have run. "Only propose ideas a simulation supported"
# was in the prompt and the model wrote a restock card before testing anything,
# then wrote a second one for the same item after being sent back. A rule that
# guards the merchant's cash belongs in code.
NEEDS_SIMULATION = {"restock": "simulate_restock", "campaign": "simulate_discount"}
NEEDS_SIMULATION_KIND = {v: k for k, v in NEEDS_SIMULATION.items()}


def create_proposal(ctx: dict[str, Any], kind: str, payload: dict[str, Any],
                    rationale: str, numbers: dict[str, Any] | None = None,
                    **_: Any) -> dict[str, Any]:
    """Write a card for the merchant. This CHANGES NOTHING - approval does."""
    if kind not in bandit.ARMS:
        return {"error": f"kind must be one of {list(bandit.ARMS)}"}

    item_id = str(payload.get("item_id") or "")
    needed = NEEDS_SIMULATION.get(kind)
    if needed:
        sim = ctx.get("simulated", {}).get((needed, item_id))
        if sim is None:
            return {"error": (f"run {needed} on '{item_id}' first - a {kind} proposal has to "
                              "carry a number the merchant can check, and nothing has tested "
                              "this one")}
        if not sim.get("worth_doing"):
            return {"error": (f"{needed} on '{item_id}' did not support this: "
                              f"{sim.get('verdict')}. Say that instead of proposing it.")}

    seen = {(p["kind"], str(p.get("payload", {}).get("item_id") or ""))
            for p in ctx["proposed"]}
    if (kind, item_id) in seen:
        return {"error": f"you already proposed a {kind} for '{item_id}' in this run"}
    with httpx.Client(base_url=ctx["shop_url"], timeout=20) as c:
        resp = c.post("/merchant/proposals",
                      json={"kind": kind, "payload": payload, "rationale": rationale,
                            "numbers": numbers or {}})
    if resp.status_code >= 400:
        return {"error": resp.json().get("why", "the shop refused the proposal")}
    prop = resp.json()
    ctx.setdefault("proposed", []).append(prop)
    return {"prop_id": prop["prop_id"], "kind": kind, "status": "open",
            "note": "Written to the console as a card. The merchant decides; nothing changed."}


REGISTRY = {
    "get_sales_metrics": get_sales_metrics,
    "get_demand_ledger": get_demand_ledger,
    "get_inventory": get_inventory,
    "get_margins": get_margins,
    "simulate_discount": simulate_discount,
    "simulate_restock": simulate_restock,
    "create_proposal": create_proposal,
}


def summarise(name: str, result: dict[str, Any]) -> str:
    if result.get("error"):
        return f"error: {result['error']}"
    if name == "get_sales_metrics":
        return f"{result['orders']} orders, {result['revenue_display']} over {result['period_days']}d"
    if name == "get_demand_ledger":
        return (f"{len(result['rows'])} refused line(s), "
                f"{result['outstanding_display']} still outstanding")
    if name == "get_inventory":
        return f"{len(result['items'])} stock rows"
    if name == "get_margins":
        return f"{len(result['items'])} items, floor {result['floor_margin_pct']}%"
    if name in ("simulate_discount", "simulate_restock"):
        return result["verdict"]
    if name == "create_proposal":
        return f"proposed {result.get('kind')} ({result.get('prop_id')})"
    return "ok"


def call_display(name: str, args: dict[str, Any]) -> str:
    rendered = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (args or {}).items())
    return f"{name}({rendered})"


# -- the run ----------------------------------------------------------------

def _system_prompt(shop: dict[str, Any], arm_order: list[str]) -> str:
    return f"""You are VelcrowAI's growth agent for {shop['name']}, a {shop['category']}
merchant. Nobody is watching this run. You wake on a schedule, look at the
merchant's own numbers, and decide whether anything is worth proposing.

YOUR STANDING GOAL
Find economically justified opportunities to increase this merchant's revenue
without violating margin policy. Propose only what the numbers support.

PRODUCING NO PROPOSAL IS A CORRECT AND COMMON OUTCOME.
If stock is healthy, nothing is being refused, and margins are tight, the right
answer is to propose nothing and say why. An agent that always finds something
is not analysing, it is performing. Do not invent work.

HOW TO WORK
- Look before you propose. get_sales_metrics, get_demand_ledger, get_inventory
  and get_margins are cheap; use the ones that matter for what you suspect.
- TEST every idea before proposing it. simulate_discount and simulate_restock
  tell you what it does to margin and what it recovers. If a simulation says
  it breaches the margin floor or does not pay for itself, DISCARD it and say
  so - a discarded idea is a good outcome, not a failure.
- SHOW YOUR WORKING BEFORE CONCLUDING NOTHING. If the demand ledger shows any
  OUTSTANDING value, simulate a restock on the worst line before deciding against it.
  If any item has margin headroom and is barely moving, simulate a discount on
  it before deciding against that. "I propose nothing" is a finding when a
  simulation supports it and merely an opinion when it does not, and your
  reasoning is read by a merchant deciding whether to trust you.
- Only then create_proposal. At most 3 in a run, and only ones a simulation
  supported.
- Never do money arithmetic yourself. Every figure you quote must come from a
  *_display string in a tool result, verbatim.

WHAT YOU MAY PROPOSE
  restock       stock that is refusing demand. Support it with simulate_restock.
  campaign      a time-boxed discount on something slow-moving with margin
                headroom. Support it with simulate_discount.
  coupon        a minimum-cart coupon to lift basket size.
  price_alert   a warning that a reorder item has changed price. No change.

Based on what this merchant has approved before, try these in order:
{", ".join(arm_order)}. That ordering is a prior, not an instruction - the
numbers decide.

GOVERNANCE
You cannot change stock or pricing. create_proposal writes a card; the merchant
approves or rejects it. Write the rationale for a busy shopkeeper: what you
found, what you propose, and the number that justifies it, in two sentences.

When you are finished, reply with a short plain summary of what you looked at
and what you decided - including deciding to do nothing."""


def _unsimulated_outstanding(ctx: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    """The worst outstanding line the agent never tested, if there is one.

    Only counts a line as tested when a simulation actually ran against that
    item, so reading the ledger and talking about it does not discharge the
    obligation.
    """
    simulated = {e["args"].get("item_id") for e in run["events"]
                 if e.get("kind") == "tool" and e.get("ok")
                 and e.get("tool") in ("simulate_restock", "simulate_discount")}
    try:
        ledger = get_demand_ledger(ctx)
    except Exception:
        return None     # the shop being unreachable is not the agent's failing
    open_rows = [r for r in ledger["rows"]
                 if r["outstanding_units"] > 0 and r["item_id"] not in simulated]
    if not open_rows:
        return None
    return max(open_rows, key=lambda r: r["outstanding_units"])


def _supported_unproposed(ctx: dict[str, Any]) -> tuple[str, str] | None:
    """The first simulation that said 'worth doing' and produced no card."""
    proposed = {(p["kind"], str(p.get("payload", {}).get("item_id") or ""))
                for p in ctx.get("proposed", [])}
    for (tool, item), sim in ctx.get("simulated", {}).items():
        kind = NEEDS_SIMULATION_KIND.get(tool)
        if not kind or not sim.get("worth_doing"):
            continue
        if (kind, item) not in proposed:
            return item, str(sim.get("verdict", ""))[:160]
    return None


def _emit(run: dict[str, Any], kind: str, **data: Any) -> None:
    run["events"].append({"seq": len(run["events"]), "kind": kind, "ts": time.time(), **data})


def run_once(shop: dict[str, Any], seed: int | None = None) -> dict[str, Any]:
    """One wake-up. Returns the run record, whatever it decided."""
    rng = random.Random(seed) if seed is not None else random.Random()
    arm_order = bandit.rank(shop["shop_id"], rng)
    ctx: dict[str, Any] = {"shop_url": shop["url"], "shop_id": shop["shop_id"], "proposed": []}
    run: dict[str, Any] = {
        "run_id": "mrun_" + uuid.uuid4().hex[:10], "shop_id": shop["shop_id"],
        "shop_name": shop["name"], "started_ts": time.time(), "events": [],
        "proposals": [], "outcome": "", "summary": "", "arm_order": arm_order,
    }
    RUNS[run["run_id"]] = run
    for stale in list(RUNS)[:-MAX_RUNS_KEPT]:
        RUNS.pop(stale, None)

    chainlog.append(shop["shop_id"], "merchant_agent_woke",
                    f"growth agent woke for {shop['name']} with nobody watching; "
                    f"standing goal, strategy order {arm_order}",
                    {"run_id": run["run_id"], "arm_order": arm_order})

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(shop, arm_order)},
        {"role": "user", "content": "Scheduled run. Look at the numbers and decide."},
    ]

    sent_back = False
    pushed_to_propose = False
    for round_no in range(MAX_ROUNDS):
        try:
            step = llm.plan(messages, tools=MERCHANT_TOOLS)
        except llm.LLMUnavailable as exc:
            _emit(run, "degraded", why=str(exc))
            run["outcome"] = "degraded"
            run["summary"] = (f"The model was unreachable ({exc}), so this run made no "
                              "proposals. Nothing was changed.")
            chainlog.append(shop["shop_id"], "merchant_agent_degraded",
                            f"growth agent could not reach the model ({exc}); no proposals made",
                            {"run_id": run["run_id"]})
            break

        if not step["tool_calls"]:
            # "Simulate the worst outstanding line before concluding nothing" was
            # in the prompt and the model skipped it anyway - it read a ledger
            # showing money being refused, wrote a paragraph about the clear
            # opportunity, and proposed nothing. Asking more firmly is not a
            # fix. The obligation is checked here instead, and an unsupported
            # "I propose nothing" is sent back once with the row it ignored.
            # A supported simulation must become a CARD or an explicit
            # discard - never prose. The model simulated the ghee restock,
            # the simulation said worth doing, and it then wrote "I propose
            # to restock" in its summary without ever calling
            # create_proposal: the console showed nothing while the text
            # claimed a proposal. A proposal that exists only as prose is
            # indistinguishable from no proposal, so the gap is closed here.
            hanging = _supported_unproposed(ctx)
            if hanging and not pushed_to_propose:
                pushed_to_propose = True
                item, verdict = hanging
                _emit(run, "sent_back", why=(
                    f"concluded with a supported simulation for '{item}' and no proposal: "
                    f"{verdict}"))
                messages.append({"role": "assistant", "content": step["content"] or None})
                messages.append({"role": "user", "content": (
                    f"You simulated '{item}' and the simulation supported it: {verdict}. "
                    "Either write it up with create_proposal NOW, or state in one sentence "
                    "why you are discarding a supported idea. Describing a proposal in "
                    "prose without creating the card leaves the merchant nothing to act "
                    "on.")})
                continue
            owed = _unsimulated_outstanding(ctx, run)
            if owed and not sent_back:
                sent_back = True
                _emit(run, "sent_back", why=(
                    f"concluded without simulating {owed['item_id']}, which the ledger "
                    f"still shows as {owed['outstanding_display']} outstanding"))
                messages.append({"role": "assistant", "content": step["content"] or None})
                messages.append({"role": "user", "content": (
                    f"You have not finished. The demand ledger still shows "
                    f"{owed['outstanding_display']} outstanding on '{owed['item_id']}'"
                    f"{' variant ' + owed['variant'] if owed['variant'] else ''}, and the "
                    f"row says it needs a {owed['action']}. Run the matching simulation on "
                    "it, then either propose or say what the simulation showed. Do not "
                    "conclude again without that number.")})
                continue
            run["summary"] = step["content"] or "No summary given."
            break

        messages.append({
            "role": "assistant", "content": step["content"] or None,
            "tool_calls": [{"id": tc["id"], "type": "function",
                            "function": {"name": tc["name"],
                                         "arguments": json.dumps(tc["args"])}}
                           for tc in step["tool_calls"]],
        })

        for tc in step["tool_calls"]:
            name, args = tc["name"], dict(tc["args"])
            why = args.pop("reason", "") or "no reason given"
            started = time.perf_counter()
            fn = REGISTRY.get(name)
            try:
                if fn is None:
                    raise ValueError(f"no such tool '{name}'")
                if name == "create_proposal" and len(ctx["proposed"]) >= 3:
                    raise ValueError("three proposals is the limit for one run")
                result = fn(ctx, **args)
                ok = not result.get("error")
                # Remember what was actually tested, so create_proposal can
                # check rather than trust. Keyed by (tool, item) because a
                # simulation of one item says nothing about another.
                if ok and name in ("simulate_restock", "simulate_discount"):
                    ctx.setdefault("simulated", {})[(name, str(args.get("item_id") or ""))] = result
            except Exception as exc:
                result, ok = {"error": str(exc)}, False
            latency = int((time.perf_counter() - started) * 1000)

            _emit(run, "tool", tool=name, args=args, why=why, ok=ok,
                  call_display=call_display(name, args),
                  result_display=f"{summarise(name, result)} · {latency}ms",
                  why_display=why, latency_ms=latency, round=round_no)
            chainlog.append(shop["shop_id"], "merchant_agent_tool",
                            f"{call_display(name, args)} -> {summarise(name, result)}; "
                            f"reason: {why}",
                            {"run_id": run["run_id"], "tool": name, "ok": ok})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(result, ensure_ascii=False)})

    run["proposals"] = ctx["proposed"]
    run["finished_ts"] = time.time()
    if not run["outcome"]:
        run["outcome"] = "proposed" if ctx["proposed"] else "no_action"

    if run["outcome"] == "no_action":
        # Spec 7.5 requires this to be a first-class result, not a silent gap.
        chainlog.append(shop["shop_id"], "merchant_agent_no_action",
                        f"growth agent looked at {shop['name']} and proposed nothing: "
                        f"{run['summary'][:220]}",
                        {"run_id": run["run_id"], "rounds": len(run["events"])})
    else:
        chainlog.append(shop["shop_id"], "merchant_agent_proposed",
                        f"growth agent made {len(ctx['proposed'])} proposal(s) for "
                        f"{shop['name']}: "
                        + "; ".join(p["rationale"][:120] for p in ctx["proposed"]),
                        {"run_id": run["run_id"],
                         "prop_ids": [p["prop_id"] for p in ctx["proposed"]]})
    return run


MERCHANT_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_sales_metrics",
        "description": "Units sold, revenue and days-of-stock per item over a period.",
        "parameters": {"type": "object", "properties": {
            "period_days": {"type": "number", "description": "How far back to look, in days"},
            "reason": llm._REASON}, "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "get_demand_ledger",
        "description": "What this shop was asked for and could not sell, valued in rupees.",
        "parameters": {"type": "object", "properties": {"reason": llm._REASON},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "get_inventory",
        "description": "Current stock per item and variant, with restock dates.",
        "parameters": {"type": "object", "properties": {"reason": llm._REASON},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "get_margins",
        "description": "Margin per item and the largest discount that respects the floor.",
        "parameters": {"type": "object", "properties": {"reason": llm._REASON},
                       "required": ["reason"]}}},
    {"type": "function", "function": {
        "name": "simulate_discount",
        "description": ("Project a discount's effect on margin BEFORE proposing it. Returns "
                        "whether it is within policy and whether it pays for itself."),
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string"},
            "pct": {"type": "number", "description": "Discount percent, e.g. 12"},
            "days": {"type": "number", "description": "How long it would run"},
            "reason": llm._REASON}, "required": ["item_id", "pct", "reason"]}}},
    {"type": "function", "function": {
        "name": "simulate_restock",
        "description": "What restocking would recover from demand already refused.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string"}, "qty": {"type": "integer"},
            "variant": {"type": "string"},
            "reason": llm._REASON}, "required": ["item_id", "qty", "reason"]}}},
    {"type": "function", "function": {
        "name": "create_proposal",
        "description": ("Write a proposal card for the merchant. Changes nothing by itself. "
                        "Only for ideas a simulation supported."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(bandit.ARMS)},
            "payload": {"type": "object",
                        "description": ("restock: {item_id, variant, qty}. campaign/coupon: "
                                        "{coupon: {code, kind, value_paise|value_pct, "
                                        "min_cart_paise, stackable, description}, days}. "
                                        "price_alert: {item_id, note}")},
            "rationale": {"type": "string", "description": "Two sentences for a busy shopkeeper"},
            "numbers": {"type": "object", "description": "The simulation output that justifies it"},
            "reason": llm._REASON},
            "required": ["kind", "payload", "rationale", "reason"]}}},
]
