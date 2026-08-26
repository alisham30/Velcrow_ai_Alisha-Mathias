"""The tool-calling loop (spec 0.8, 6.3). The model is stubbed so the loop
itself is under test: that it executes whatever tool comes back, feeds the
result in, and keeps going until the model answers instead of calling a tool.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent import llm, runtime, tools
from common import chainlog, mandate


@pytest.fixture
def shop_ctx(freshkart, monkeypatch):
    """Point the agent's HTTP calls at the in-process shop."""
    class FakeClient:
        def __init__(self, base_url: str, timeout: int = 0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path: str):
            return freshkart.get(path)

        def post(self, path: str, json=None, headers=None):
            return freshkart.post(path, json=json, headers=headers)

        def patch(self, path: str, json=None):
            return freshkart.patch(path, json=json)

    monkeypatch.setattr(tools, "_client", lambda url: FakeClient(url))
    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)
    cart_id = freshkart.post("/cart").json()["cart_id"]
    return {"shop": {"shop_id": "freshkart", "name": "FreshKart", "category": "grocery",
                     "url": "http://testshop"},
            "cart_id": cart_id, "client": freshkart}


def _token() -> str:
    return mandate.issue(500000, 300000, ["freshkart"])


def _claims() -> dict[str, Any]:
    return mandate.verify(_token())


def _scripted(*turns):
    """Return a plan() stand-in that walks a fixed list of model replies."""
    seq = list(turns)
    calls: list[list[dict[str, Any]]] = []

    def fake_plan(messages):
        calls.append(messages)
        return seq.pop(0)

    fake_plan.messages_seen = calls
    return fake_plan


def _run(run, shop_ctx, text, claims=None):
    # the shop verifies the mandate itself before touching stock, so the token
    # has to travel with the turn as it does in the real service
    token = _token()
    asyncio.run(runtime.run_turn(run, shop_ctx["shop"], shop_ctx["cart_id"], text, [],
                                 claims or mandate.verify(token), mandate_token=token))


def test_loop_executes_tools_then_stops_on_a_text_answer(shop_ctx, monkeypatch):
    plan = _scripted(
        {"content": "", "tool_calls": [{"id": "c1", "name": "search_catalog",
                                        "args": {"query": "lemons", "max_price_paise": 10000,
                                                 "reason": "find lemons under Rs 100"}}]},
        {"content": "", "tool_calls": [{"id": "c2", "name": "add_to_cart",
                                        "args": {"item_id": "lemons-1kg", "qty": 2,
                                                 "reason": "add the 1 kg pack twice"}}]},
        {"content": "Added 2 kg of lemons for Rs 88.00.", "tool_calls": []},
    )
    monkeypatch.setattr(llm, "plan", plan)
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add 2 kg lemons under Rs 100")

    kinds = [e["kind"] for e in run.events]
    assert kinds.count("tool") == 2
    assert kinds[-2:] == ["message", "cart_changed"]
    used = [e["tool"] for e in run.events if e["kind"] == "tool"]
    assert used == ["search_catalog", "add_to_cart"]
    # the tool actually moved the page's cart
    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert cart["items"][0]["item_id"] == "lemons-1kg" and cart["items"][0]["qty"] == 2
    assert run.events[-1]["changed"] is True


def test_round_count_varies_with_what_the_model_finds(shop_ctx, monkeypatch):
    """A one-tool turn and a three-tool turn on the same loop — nothing here
    fixes the sequence."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "a", "name": "view_cart",
                                        "args": {"reason": "check the basket"}}]},
        {"content": "Your basket is empty.", "tool_calls": []},
    ))
    short = runtime.new_run("freshkart")
    _run(short, shop_ctx, "what is in my cart")

    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "b", "name": "search_catalog",
                                        "args": {"query": "honey", "reason": "find honey"}}]},
        {"content": "", "tool_calls": [{"id": "c", "name": "add_to_cart",
                                        "args": {"item_id": "honey-500g", "qty": 1,
                                                 "reason": "add one jar"}}]},
        {"content": "", "tool_calls": [{"id": "d", "name": "view_cart",
                                        "args": {"reason": "confirm the total"}}]},
        {"content": "Added honey; basket is Rs 219.00.", "tool_calls": []},
    ))
    long = runtime.new_run("freshkart")
    _run(long, shop_ctx, "add honey and tell me the total")

    assert len([e for e in short.events if e["kind"] == "tool"]) == 1
    assert len([e for e in long.events if e["kind"] == "tool"]) == 3


def test_tool_failure_is_fed_back_so_the_model_can_recover(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "x", "name": "add_to_cart",
                                        "args": {"item_id": "ghee-500ml", "qty": 1,
                                                 "reason": "shopper asked for ghee"}}]},
        {"content": "Ghee is out of stock until 2026-08-28.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add ghee")
    tool_event = next(e for e in run.events if e["kind"] == "tool")
    assert tool_event["ok"] is False
    assert "0 in stock" in tool_event["result_summary"]
    assert run.events[-1]["changed"] is False  # nothing was added


def test_every_tool_call_is_chain_logged_with_the_models_reason(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "c1", "name": "search_catalog",
                                        "args": {"query": "lemons",
                                                 "reason": "the shopper asked for lemons"}}]},
        {"content": "Found them.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "find lemons")
    entry = next(e for e in reversed(chainlog.tail("buyer", 10))
                 if e["event"] == "agent_tool_call")
    assert entry["data"]["tool"] == "search_catalog"
    assert "the shopper asked for lemons" in entry["why"]
    ok, _ = chainlog.verify_chain("buyer")
    assert ok


def test_dead_api_degrades_instead_of_killing_the_turn(shop_ctx, monkeypatch):
    def boom(messages):
        raise llm.LLMUnavailable("simulated outage")

    monkeypatch.setattr(llm, "plan", boom)
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add 2 kg lemons under Rs 100")

    assert any(e["kind"] == "degraded" for e in run.events)
    final = next(e for e in run.events if e["kind"] == "message")
    assert final["degraded"] is True
    assert "offline" in final["text"].lower()
    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert cart["items"] and cart["items"][0]["item_id"] == "lemons-1kg"


def test_runaway_loop_is_bounded(shop_ctx, monkeypatch):
    always_tool = {"content": "", "tool_calls": [{"id": "z", "name": "view_cart",
                                                  "args": {"reason": "looking again"}}]}
    monkeypatch.setattr(llm, "plan", lambda messages: dict(always_tool))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "loop forever")
    assert len([e for e in run.events if e["kind"] == "tool"]) == llm.MAX_ROUNDS
    assert run.events[-2]["kind"] == "message"


def test_sse_subscriber_replays_a_finished_run(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted({"content": "Nothing to do.", "tool_calls": []}))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "hello")

    async def drain():
        q = run.subscribe()
        seen = []
        while True:
            item = await q.get()
            if item is None:
                return seen
            seen.append(item)

    assert [e["kind"] for e in asyncio.run(drain())] == [e["kind"] for e in run.events]


def test_tool_results_carry_preformatted_money(shop_ctx):
    """The model must never convert paise to rupees itself (it slipped a
    decimal place once). Every amount it can quote is preformatted for it."""
    from common.money import rupees

    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart", "cart_id": shop_ctx["cart_id"],
           "mandate_token": _token()}   # the shop verifies it before touching stock
    found = tools.search_catalog(ctx, query="lemons")
    assert found["matches"][0]["price_display"] == rupees(4400)

    cart = tools.add_to_cart(ctx, item_id="lemons-1kg", qty=2, variant="")
    assert cart["subtotal_display"] == rupees(8800)
    line = cart["lines"][0]
    assert line["unit_price_display"] == rupees(4400)
    assert line["line_total_display"] == rupees(8800)


def test_prompt_states_money_in_rupees_not_bare_paise(shop_ctx):
    """Nothing in the live context invites the model to do the conversion."""
    prompt = llm.system_prompt({
        "shop_name": "FreshKart", "shop_category": "grocery", "shop_id": "freshkart",
        "categories": ["produce"], "catalog_size": 11, "variant_kind": "pack",
        "cart_lines": [{"line_id": "l1", "name": "Lemons", "variant": "",
                        "qty": 2, "unit_price_paise": 4400}],
        "cart_count": 2, "subtotal_paise": 8800,
        "max_per_txn_paise": 300000, "max_total_paise": 500000,
    })
    assert "₹88.00" in prompt and "₹3,000.00" in prompt
    assert "8800 paise" not in prompt
    assert "NEVER do arithmetic on money" in prompt


def test_prompt_requires_asking_rather_than_guessing_qty_or_size():
    """An unstated quantity or size must produce a question, not a silent
    default of 1 (or a size picked for the shopper)."""
    prompt = llm.system_prompt({
        "shop_name": "FreshKart", "shop_category": "grocery", "shop_id": "freshkart",
        "categories": ["produce"], "catalog_size": 11, "variant_kind": "pack",
        "cart_lines": [], "cart_count": 0, "subtotal_paise": 0,
        "max_per_txn_paise": 300000, "max_total_paise": 500000,
    })
    assert "do NOT call add_to_cart on" in prompt
    assert "ask how many" in prompt and "ask which size" in prompt
    # ...but a stated quantity must still go straight through
    assert "-> add it" in prompt


def test_same_product_cannot_be_added_twice_in_one_turn(shop_ctx, monkeypatch):
    """A model that misreads its own question must not silently double the
    basket. The guard is in code, not in the prompt (spec 7.9)."""
    add = {"id": "a", "name": "add_to_cart",
           "args": {"item_id": "basmati-5kg", "qty": 1, "reason": "shopper agreed"}}
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [dict(add)]},
        {"content": "", "tool_calls": [dict(add, id="b")]},   # the double-add
        {"content": "It is already in your cart.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "okayy add")

    calls = [e for e in run.events if e["kind"] == "tool"]
    assert calls[0]["ok"] is True
    assert calls[1]["ok"] is False
    assert "already added in this turn" in calls[1]["result_summary"]
    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert sum(l["qty"] for l in cart["items"]) == 1  # not 2


def test_different_products_may_both_be_added_in_one_turn(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [
            {"id": "a", "name": "add_to_cart",
             "args": {"item_id": "lemons-1kg", "qty": 2, "reason": "first item"}},
            {"id": "b", "name": "add_to_cart",
             "args": {"item_id": "honey-500g", "qty": 1, "reason": "second item"}}]},
        {"content": "Added both.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add lemons and honey")
    assert all(e["ok"] for e in run.events if e["kind"] == "tool")
    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert len(cart["items"]) == 2


def test_conversation_carries_tool_results_into_the_next_turn(shop_ctx, monkeypatch):
    """The model must be able to reuse an item_id it already looked up, rather
    than inventing one on the next turn."""
    runtime.CONVERSATIONS.clear()
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "s1", "name": "search_catalog",
                                        "args": {"query": "basmati", "reason": "find rice"}}]},
        {"content": "Only a 5 kg pack. Add it?", "tool_calls": []},
    ))
    run1 = runtime.new_run("freshkart")
    _run(run1, shop_ctx, "1 kg basmati rice")

    seen: list[list[dict]] = []
    monkeypatch.setattr(llm, "plan", lambda m: (seen.append(m), {
        "content": "Added.", "tool_calls": []})[1])
    run2 = runtime.new_run("freshkart")
    _run(run2, shop_ctx, "okayy add")

    roles = [m["role"] for m in seen[0]]
    assert "tool" in roles, "previous tool results were not carried forward"
    blob = json.dumps(seen[0])
    assert "basmati-5kg" in blob  # the real id is in front of the model
    assert seen[0][0]["role"] == "system"  # context rebuilt fresh each turn


def test_conversation_is_per_cart(shop_ctx, monkeypatch):
    runtime.CONVERSATIONS.clear()
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "First cart.", "tool_calls": []},
        {"content": "Second cart.", "tool_calls": []},
    ))
    _run(runtime.new_run("freshkart"), shop_ctx, "hello")
    other = dict(shop_ctx, cart_id=shop_ctx["client"].post("/cart").json()["cart_id"])
    _run(runtime.new_run("freshkart"), other, "hello")
    assert len(runtime.CONVERSATIONS) == 2


def test_search_matches_across_singular_and_plural(shop_ctx):
    """The shopper's word rarely matches the shop's word exactly."""
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart", "cart_id": shop_ctx["cart_id"]}
    veg = tools.search_catalog(ctx, query="vegetables")  # catalog tag is "vegetable"
    assert [m["name"] for m in veg["matches"]] == ["Tomatoes (1 kg)"]
    assert tools.search_catalog(ctx, query="lemon")["match_count"] == 1   # catalog says "Lemons"
    assert tools.search_catalog(ctx, query="tomatoes")["match_count"] == 1


def test_empty_search_returns_the_whole_shelf_not_a_denial(shop_ctx):
    """A word this shop does not use must never become 'we sell none of that'."""
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart", "cart_id": shop_ctx["cart_id"]}
    result = tools.search_catalog(ctx, query="veggies")
    assert result["match_count"] == 0
    overview = result["catalog_overview"]
    assert len(overview) == 11
    tomato = next(p for p in overview if p["item_id"] == "tomatoes-1kg")
    assert tomato["tags"] == ["vegetable", "fresh"] and tomato["price_display"] == "₹32.00"
    assert "do not tell the shopper the shop has none" in result["note"]


def test_successful_search_does_not_ship_the_whole_catalog(shop_ctx):
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart", "cart_id": shop_ctx["cart_id"]}
    assert "catalog_overview" not in tools.search_catalog(ctx, query="lemons")


# -- the reasoning strip (spec 6.5) ----------------------------------------
# The strip under each reply is the proof the agent chose its own actions, so
# it is treated as a product surface: every line it shows is built server-side
# and asserted complete here. A blank bullet would silently read as "the agent
# had no reason", which is exactly the claim the strip exists to defend.

def test_every_tool_event_carries_a_complete_display_line(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "c1", "name": "search_catalog",
                                        "args": {"query": "lemons", "max_price_paise": 10000,
                                                 "reason": "find lemons under the stated ceiling"}}]},
        {"content": "", "tool_calls": [{"id": "c2", "name": "add_to_cart",
                                        "args": {"item_id": "lemons-1kg", "qty": 2,
                                                 "reason": "add the 1 kg pack twice"}}]},
        {"content": "Added 2 kg of lemons.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add 2 kg lemons under Rs 100")

    steps = [e for e in run.events if e["kind"] == "tool"]
    assert len(steps) == 2
    for step in steps:
        for field in ("call_display", "result_display", "why_display"):
            assert field in step, f"{field} missing from the trace event"
            assert step[field].strip(), f"{field} is blank; the strip would render an empty bullet"

    assert steps[0]["call_display"] == 'search_catalog(query="lemons", max_price_paise=10000)'
    assert steps[0]["result_display"].startswith("1 match(es), best Lemons (1 kg) at ")
    assert steps[0]["why_display"] == "find lemons under the stated ceiling"
    assert steps[1]["call_display"] == 'add_to_cart(item_id="lemons-1kg", qty=2)'
    assert "ms" in steps[1]["result_display"]


def test_a_reasonless_tool_call_still_renders_a_readable_line(shop_ctx, monkeypatch):
    """The model can omit `reason`. The strip must say so, not go blank."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "c1", "name": "view_cart", "args": {}}]},
        {"content": "Your basket is empty.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "what is in my cart")

    step = next(e for e in run.events if e["kind"] == "tool")
    assert step["call_display"] == "view_cart()"
    assert step["why_display"] == "no reason given"
    assert step["result_display"].strip()


def test_a_refused_tool_call_still_carries_its_display_line(shop_ctx, monkeypatch):
    """A failure is part of the reasoning: the strip shows what was tried."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "x", "name": "add_to_cart",
                                        "args": {"item_id": "ghee-500ml", "qty": 1,
                                                 "reason": "shopper asked for ghee"}}]},
        {"content": "Ghee is out of stock.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "add ghee")

    step = next(e for e in run.events if e["kind"] == "tool")
    assert step["ok"] is False
    assert step["call_display"] == 'add_to_cart(item_id="ghee-500ml", qty=1)'
    assert "refused" in step["result_display"]
    assert step["why_display"] == "shopper asked for ghee"


def test_money_in_the_strip_is_the_rupee_sign_not_mojibake(shop_ctx, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "c1", "name": "search_catalog",
                                        "args": {"query": "lemons", "reason": "find lemons"}}]},
        {"content": "Found them.", "tool_calls": []},
    ))
    run = runtime.new_run("freshkart")
    _run(run, shop_ctx, "find lemons")

    step = next(e for e in run.events if e["kind"] == "tool")
    assert "\u20b9" in step["result_display"]
    assert "\u00e2" not in step["result_display"]  # the cp1252 mojibake lead byte
