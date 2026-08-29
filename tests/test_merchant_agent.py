"""Phase 8b: the autonomous merchant agent (spec 7.5) and the bandit (4.6).

This is the only part of the system that runs with nobody present, so the
tests care most about the things that make that safe and honest:

- it cannot change stock or pricing, only propose
- producing NO proposal is a first-class outcome, logged with its reasoning
- it must simulate before it proposes, so a number in a rationale traces back
  to a tool result rather than to the model's imagination
- the margin floor is enforced in code, not by asking the model nicely
- a merchant's decision is the training signal, and it actually moves the
  posterior that decides what gets tried first next time
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agent import llm, merchant
from common import bandit, chainlog, mandate


@pytest.fixture
def loom(loomcraft, monkeypatch):
    """Route the agent's outbound HTTP at the in-process shop."""
    class Routed:
        @staticmethod
        def _split(url: str) -> str:
            return url.replace("http://testshop", "") or "/"

        class Client:
            def __init__(self, base_url: str, timeout: int = 0) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, path: str, params: dict[str, Any] | None = None):
                return loomcraft.get(path, params=params)

            def post(self, path: str, json=None, **kw):
                return loomcraft.post(path, json=json)

        @staticmethod
        def post(url: str, json=None, timeout: int = 0):
            return loomcraft.post(Routed._split(url), json=json)

    monkeypatch.setattr(merchant, "httpx", Routed)
    return {"shop_id": "loomcraft", "name": "Loomcraft", "category": "apparel",
            "url": "http://testshop", "client": loomcraft}


def _scripted(*turns):
    seq = list(turns)

    def plan(messages, tools=None):
        return seq.pop(0) if seq else {"content": "Done.", "tool_calls": []}

    return plan


def _refuse_demand(client, item_id: str, variant: str, qty: int) -> None:
    """Make the shop actually turn a shopper away, so the ledger is real."""
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    cart = client.post("/cart").json()["cart_id"]
    client.post(f"/cart/{cart}/fulfil",
                json={"item_id": item_id, "variant": variant, "qty": qty,
                      "contact_ref": "z@example.com"},
                headers={"Authorization": f"Mandate {token}"}).raise_for_status()


# -- it runs with nobody there ----------------------------------------------

def test_a_run_needs_no_shopper_and_is_chain_logged(loom, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "Nothing worth doing.", "tool_calls": []}))
    run = merchant.run_once(loom)

    assert run["outcome"] == "no_action"
    events = [e["event"] for e in chainlog.tail("loomcraft", 40)]
    assert "merchant_agent_woke" in events
    assert "merchant_agent_no_action" in events


def test_proposing_nothing_is_a_first_class_outcome(loom, monkeypatch):
    """Spec 7.5: an agent that always finds something is a script."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "a", "name": "get_demand_ledger",
                                        "args": {"reason": "check for refused demand"}}]},
        {"content": "Stock is healthy and margins are tight. I propose nothing.",
         "tool_calls": []},
    ))
    run = merchant.run_once(loom)

    assert run["outcome"] == "no_action"
    assert run["proposals"] == []
    entry = next(e for e in chainlog.tail("loomcraft", 40)
                 if e["event"] == "merchant_agent_no_action")
    assert "proposed nothing" in entry["why"]


def test_it_cannot_change_stock_or_pricing_itself(loom):
    """Every tool it has either reads, simulates, or writes a proposal card."""
    import inspect

    for name, fn in merchant.REGISTRY.items():
        src = inspect.getsource(fn)
        assert "/admin/restock" not in src, f"{name} can restock directly"
        assert "adjust_stock" not in src, f"{name} can move stock directly"
    assert set(merchant.REGISTRY) == {
        "get_sales_metrics", "get_demand_ledger", "get_inventory", "get_margins",
        "simulate_discount", "simulate_restock", "create_proposal"}


def test_a_proposal_changes_nothing_until_a_human_decides(loom, monkeypatch):
    item_id, variant = "kurti-indigo-cotton", "S"
    before = next(v["stock"] for p in loom["client"].get("/catalog").json()
                  if p["id"] == item_id for v in p["variants"] if v["label"] == variant)

    # The refusal has to be real, and the simulation has to have run: a restock
    # proposal no longer reaches the merchant without one.
    _refuse_demand(loom["client"], item_id, variant, 3)
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "s", "name": "simulate_restock",
                                        "args": {"item_id": item_id, "variant": variant,
                                                 "qty": 3, "reason": "is it worth it"}}]},
        {"content": "", "tool_calls": [{"id": "a", "name": "create_proposal",
                                        "args": {"kind": "restock",
                                                 "payload": {"item_id": item_id,
                                                             "variant": variant, "qty": 3},
                                                 "rationale": "recovers refused demand",
                                                 "reason": "worth restocking"}}]},
        {"content": "Proposed.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert run["outcome"] == "proposed"

    after = next(v["stock"] for p in loom["client"].get("/catalog").json()
                 if p["id"] == item_id for v in p["variants"] if v["label"] == variant)
    assert after == before, "a proposal moved stock before anyone approved it"
    assert loom["client"].get("/merchant/proposals?status=open").json()["proposals"]


# -- simulations are real, and the floor is code ----------------------------

def test_a_restock_simulation_values_demand_actually_refused(loom):
    _refuse_demand(loom["client"], "kurti-indigo-cotton", "S", 40)
    ctx = {"shop_url": "http://testshop", "shop_id": "loomcraft"}
    sim = merchant.simulate_restock(ctx, item_id="kurti-indigo-cotton", qty=10, variant="S")

    assert sim["demand_refused"] > 0
    assert sim["recoverable_units"] > 0
    assert sim["worth_doing"] is True
    assert sim["revenue_display"].startswith("₹")


def test_restocking_something_nobody_asked_for_is_not_worth_doing(loom):
    ctx = {"shop_url": "http://testshop", "shop_id": "loomcraft"}
    sim = merchant.simulate_restock(ctx, item_id="tshirt-graphic-black", qty=50)
    assert sim["recoverable_units"] == 0
    assert sim["worth_doing"] is False
    assert "recovers no lost sale" in sim["verdict"]


def test_the_margin_floor_is_enforced_in_code_not_in_the_prompt(loom):
    """The model can ask for any discount it likes. The simulation is what
    refuses, so no amount of persuasion gets past the floor."""
    ctx = {"shop_url": "http://testshop", "shop_id": "loomcraft"}
    reckless = merchant.simulate_discount(ctx, item_id="kurti-indigo-cotton", pct=90)
    assert reckless["within_policy"] is False
    assert "breaches" in reckless["verdict"]
    assert reckless["max_safe_pct"] < 90


def test_a_discount_simulation_states_its_assumption(loom):
    """A projection that hides its elasticity is a forecast pretending to be
    data. It has to say so where the model - and the merchant - can see it."""
    ctx = {"shop_url": "http://testshop", "shop_id": "loomcraft"}
    sim = merchant.simulate_discount(ctx, item_id="kurti-indigo-cotton", pct=5)
    assert "best case, not data" in sim["assumption"]
    assert sim["margin_delta_display"].startswith("₹") or sim["margin_delta_display"].startswith("-")


def test_at_most_three_proposals_come_out_of_one_run(loom, monkeypatch):
    # price_alert changes no money, so it needs no simulation - which keeps this
    # test about the cap rather than about the simulation gate. Five different
    # items, because proposing the same one twice is refused separately.
    items = ["kurti-indigo-cotton", "shirt-oxford-white", "tshirt-graphic-black",
             "hoodie-fleece-grey", "jeans-slim-indigo"]
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [
            {"id": f"c{i}", "name": "create_proposal",
             "args": {"kind": "price_alert", "payload": {"item_id": item, "note": "up"},
                      "rationale": "r", "reason": "why"}}
            for i, item in enumerate(items)]},
        {"content": "Done.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert len(run["proposals"]) == 3
    refused = [e for e in run["events"] if e["kind"] == "tool" and not e["ok"]]
    assert refused and "limit" in refused[0]["result_display"]


# -- the governance gate ----------------------------------------------------

def test_approving_applies_the_change_and_rejecting_does_not(loom):
    client = loom["client"]
    prop = client.post("/merchant/proposals", json={
        "kind": "restock", "payload": {"item_id": "kurti-indigo-cotton", "variant": "S",
                                       "qty": 7},
        "rationale": "recovers refused demand", "numbers": {}}).json()

    before = next(v["stock"] for p in client.get("/catalog").json()
                  if p["id"] == "kurti-indigo-cotton" for v in p["variants"]
                  if v["label"] == "S")
    client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                json={"decision": "approve", "reason": "yes"}).raise_for_status()
    after = next(v["stock"] for p in client.get("/catalog").json()
                 if p["id"] == "kurti-indigo-cotton" for v in p["variants"]
                 if v["label"] == "S")
    assert after == before + 7

    rejected = client.post("/merchant/proposals", json={
        "kind": "restock", "payload": {"item_id": "kurti-indigo-cotton", "variant": "S",
                                       "qty": 50},
        "rationale": "too many", "numbers": {}}).json()
    client.post(f"/merchant/proposals/{rejected['prop_id']}/decide",
                json={"decision": "reject", "reason": "too much cash"}).raise_for_status()
    unchanged = next(v["stock"] for p in client.get("/catalog").json()
                     if p["id"] == "kurti-indigo-cotton" for v in p["variants"]
                     if v["label"] == "S")
    assert unchanged == after


def test_an_approved_campaign_actually_changes_what_shoppers_pay(loom):
    """Approval that does not reach the till is theatre."""
    client = loom["client"]
    prop = client.post("/merchant/proposals", json={
        "kind": "campaign",
        "payload": {"coupon": {"code": "AGENT500", "kind": "flat", "value_paise": 50000,
                               "min_cart_paise": 0, "stackable": False,
                               "description": "Agent campaign"},
                    "days": 7},
        "rationale": "slow mover with headroom", "numbers": {}}).json()
    client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                json={"decision": "approve"}).raise_for_status()

    cart = client.post("/cart").json()["cart_id"]
    client.patch(f"/cart/{cart}", json={"op": "add", "item_id": "kurti-indigo-cotton",
                                        "variant": "M", "qty": 1}).raise_for_status()
    quote = client.post(f"/cart/{cart}/coupons", json={}).json()
    # live in the optimiser, and winning because it is genuinely the better one
    assert any(a["code"] == "AGENT500" for a in quote["applicable"])
    assert quote["best"]["codes"] == ["AGENT500"]


def test_a_campaign_coupon_still_loses_to_a_better_standing_one(loom):
    """Approving a campaign puts it in the running; it does not let it win.
    The optimiser still gives the shopper whichever is actually cheapest."""
    client = loom["client"]
    prop = client.post("/merchant/proposals", json={
        "kind": "campaign",
        "payload": {"coupon": {"code": "AGENTTINY", "kind": "flat", "value_paise": 100,
                               "min_cart_paise": 0, "stackable": False,
                               "description": "A worse offer"}, "days": 7},
        "rationale": "r", "numbers": {}}).json()
    client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                json={"decision": "approve"}).raise_for_status()

    cart = client.post("/cart").json()["cart_id"]
    client.patch(f"/cart/{cart}", json={"op": "add", "item_id": "kurti-indigo-cotton",
                                        "variant": "M", "qty": 1}).raise_for_status()
    quote = client.post(f"/cart/{cart}/coupons", json={}).json()
    assert any(a["code"] == "AGENTTINY" for a in quote["applicable"])
    assert "AGENTTINY" not in quote["best"]["codes"]


def test_a_decision_cannot_be_made_twice(loom):
    client = loom["client"]
    prop = client.post("/merchant/proposals", json={
        "kind": "price_alert", "payload": {"item_id": "x", "note": "n"},
        "rationale": "r", "numbers": {}}).json()
    client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                json={"decision": "approve"}).raise_for_status()
    again = client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                        json={"decision": "reject"})
    assert again.json()["code"] == "IDEMPOTENT_REPLAY"


def test_both_decisions_are_chain_logged_with_the_reason(loom):
    client = loom["client"]
    prop = client.post("/merchant/proposals", json={
        "kind": "restock", "payload": {"item_id": "kurti-indigo-cotton", "qty": 2},
        "rationale": "r", "numbers": {}}).json()
    client.post(f"/merchant/proposals/{prop['prop_id']}/decide",
                json={"decision": "reject", "reason": "cash is tight this month"})
    entry = next(e for e in chainlog.tail("loomcraft", 30) if e["event"] == "proposal_rejectd")
    assert "cash is tight this month" in entry["why"]
    assert "nothing was changed" in entry["why"]


# -- the bandit (spec 4.6) --------------------------------------------------

def test_every_arm_starts_at_a_uniform_prior():
    st = bandit.state("nobody")
    assert set(st) == set(bandit.ARMS)
    for arm in bandit.ARMS:
        assert st[arm]["alpha"] == 1 and st[arm]["beta"] == 1
        assert st[arm]["mean"] == 0.5


def test_approval_and_rejection_move_the_posterior_in_opposite_directions():
    bandit.record("shopA", "restock", accepted=True)
    bandit.record("shopA", "campaign", accepted=False)
    st = bandit.state("shopA")
    assert st["restock"]["alpha"] == 2 and st["restock"]["approvals"] == 1
    assert st["campaign"]["beta"] == 2 and st["campaign"]["rejections"] == 1
    assert st["restock"]["mean"] > st["campaign"]["mean"]


def test_a_strategy_the_merchant_keeps_rejecting_stops_being_led_with():
    """The point of the bandit: preference is learned from decisions, not set
    by me. With a lopsided history the winner should dominate the draws."""
    for _ in range(25):
        bandit.record("shopB", "restock", accepted=True)
        bandit.record("shopB", "campaign", accepted=False)

    import random
    firsts = [bandit.rank("shopB", random.Random(seed))[0] for seed in range(40)]
    assert firsts.count("restock") > firsts.count("campaign") * 3


def test_ranking_is_deterministic_for_a_given_seed():
    import random
    assert bandit.rank("shopC", random.Random(7)) == bandit.rank("shopC", random.Random(7))


def test_an_unknown_arm_is_ignored_rather_than_stored():
    assert bandit.record("shopD", "not_a_strategy", accepted=True) == {}
    assert set(bandit.state("shopD")) == set(bandit.ARMS)


# -- Phase 9: what the agent may say, checked in code -------------------------

def test_a_restock_proposal_without_a_simulation_is_refused(loom, monkeypatch):
    """The prompt asked for this and the model ignored it - it wrote a restock
    card before testing anything, then a second one for the same item after
    being sent back. The merchant's cash is not protected by a paragraph."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "a", "name": "create_proposal",
                                        "args": {"kind": "restock",
                                                 "payload": {"item_id": "kurti-indigo-cotton",
                                                             "qty": 5},
                                                 "rationale": "feels right",
                                                 "reason": "hunch"}}]},
        {"content": "Fine, nothing then.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert run["outcome"] == "no_action"
    assert not run["proposals"]
    refused = [e for e in run["events"] if e["kind"] == "tool" and not e["ok"]]
    assert refused and "simulate_restock" in refused[0]["result_display"]


def test_a_simulation_that_says_no_cannot_be_proposed_anyway(loom, monkeypatch):
    """Nothing was refused for this item, so the simulation says a restock
    recovers nothing. Proposing it regardless has to fail."""
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "s", "name": "simulate_restock",
                                        "args": {"item_id": "socks-crew-3pack", "qty": 10,
                                                 "reason": "checking"}}]},
        {"content": "", "tool_calls": [{"id": "a", "name": "create_proposal",
                                        "args": {"kind": "restock",
                                                 "payload": {"item_id": "socks-crew-3pack",
                                                             "qty": 10},
                                                 "rationale": "more socks",
                                                 "reason": "why not"}}]},
        {"content": "Nothing then.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert not run["proposals"]
    refused = [e for e in run["events"] if e["kind"] == "tool" and not e["ok"]]
    assert refused and "did not support this" in refused[0]["result_display"]


def test_the_same_proposal_cannot_be_written_twice_in_one_run(loom, monkeypatch):
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [
            {"id": f"c{i}", "name": "create_proposal",
             "args": {"kind": "price_alert", "payload": {"item_id": "socks-crew-3pack",
                                                         "note": "up"},
                      "rationale": "r", "reason": "why"}} for i in range(2)]},
        {"content": "Done.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert len(run["proposals"]) == 1
    refused = [e for e in run["events"] if e["kind"] == "tool" and not e["ok"]]
    assert refused and "already proposed" in refused[0]["result_display"]


def test_concluding_nothing_with_demand_untested_is_sent_back(loom, monkeypatch):
    """'Show your working before concluding nothing' was in the prompt and the
    model skipped it, reading a ledger full of refused money and then writing a
    paragraph about the clear opportunity it was not going to act on."""
    _refuse_demand(loom["client"], "kurti-indigo-cotton", "S", 4)
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "d", "name": "get_demand_ledger",
                                        "args": {"reason": "looking"}}]},
        {"content": "Nothing to do here.", "tool_calls": []},
        {"content": "", "tool_calls": [{"id": "s", "name": "simulate_restock",
                                        "args": {"item_id": "kurti-indigo-cotton",
                                                 "variant": "S", "qty": 4,
                                                 "reason": "sent back, testing it"}}]},
        {"content": "Tested it - here is what it showed.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    sent_back = [e for e in run["events"] if e["kind"] == "sent_back"]
    assert sent_back, "the agent was allowed to conclude without testing anything"
    assert "kurti-indigo-cotton" in sent_back[0]["why"]
    assert any(e.get("tool") == "simulate_restock" for e in run["events"])


def test_the_agent_is_only_sent_back_once(loom, monkeypatch):
    """A model that will not simulate must not be looped forever - one push,
    then its answer stands and the trace shows it was pushed."""
    _refuse_demand(loom["client"], "kurti-indigo-cotton", "S", 4)
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "Nothing to do.", "tool_calls": []},
        {"content": "Still nothing.", "tool_calls": []},
        {"content": "Still nothing.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert len([e for e in run["events"] if e["kind"] == "sent_back"]) == 1
    assert run["outcome"] == "no_action"
    assert run["summary"] == "Still nothing."


def test_a_restock_is_not_proposed_for_stock_already_on_the_shelf(loom):
    """The simulation used to ignore inventory, so it told the agent to buy
    three more sarees while four sat in the stockroom. The shopper had been
    refused before the shelf was refilled; what is missing is a message."""
    on_hand = next(p["stock"] for p in loom["client"].get("/catalog").json()
                   if p["id"] == "socks-crew-3pack")
    _refuse_demand(loom["client"], "socks-crew-3pack", "", on_hand + 3)   # short by 3
    loom["client"].post("/admin/restock",
                        json={"item_id": "socks-crew-3pack", "variant": "", "qty": 3})
    ctx = {"shop_url": "http://testshop", "shop_id": "loomcraft"}
    sim = merchant.simulate_restock(ctx, item_id="socks-crew-3pack", qty=3)
    assert sim["worth_doing"] is False
    assert sim["recoverable_by_telling_them"] > 0
    assert "ALREADY on the shelf" in sim["verdict"]


def test_a_supported_simulation_cannot_end_as_prose(loom, monkeypatch):
    """Found in the phase-13 full check: the model simulated a restock, the
    simulation said worth doing, and it then wrote 'I propose to restock' in
    its SUMMARY without ever calling create_proposal - the console showed
    nothing while the text claimed a proposal. A supported simulation must
    become a card or an explicit discard, and the push is code."""
    _refuse_demand(loom["client"], "kurti-indigo-cotton", "S", 3)
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "s", "name": "simulate_restock",
                                        "args": {"item_id": "kurti-indigo-cotton",
                                                 "variant": "S", "qty": 3,
                                                 "reason": "testing the hole"}}]},
        {"content": "I propose restocking the kurti. It is clearly worth it.",
         "tool_calls": []},                                   # prose, no card
        {"content": "", "tool_calls": [{"id": "p", "name": "create_proposal",
                                        "args": {"kind": "restock",
                                                 "payload": {"item_id": "kurti-indigo-cotton",
                                                             "variant": "S", "qty": 3},
                                                 "rationale": "recovers refused demand",
                                                 "reason": "pushed to write the card"}}]},
        {"content": "Proposed.", "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert run["outcome"] == "proposed"
    assert len(run["proposals"]) == 1
    pushes = [e for e in run["events"] if e["kind"] == "sent_back"]
    assert any("no proposal" in e["why"] for e in pushes)


def test_an_explicit_discard_after_the_push_stands(loom, monkeypatch):
    """The push happens once. A model that then states its discard keeps its
    answer - the gate forces the choice into the open, not a particular one."""
    _refuse_demand(loom["client"], "kurti-indigo-cotton", "S", 3)
    monkeypatch.setattr(llm, "plan", _scripted(
        {"content": "", "tool_calls": [{"id": "s", "name": "simulate_restock",
                                        "args": {"item_id": "kurti-indigo-cotton",
                                                 "variant": "S", "qty": 3,
                                                 "reason": "testing"}}]},
        {"content": "Worth doing on paper.", "tool_calls": []},
        {"content": "Discarding: the supplier lead time makes this moot.",
         "tool_calls": []},
    ))
    run = merchant.run_once(loom)
    assert run["outcome"] == "no_action"
    assert "Discarding" in run["summary"]
    assert len([e for e in run["events"] if e["kind"] == "sent_back"]) == 1
