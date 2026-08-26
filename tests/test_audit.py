"""The evidence room (spec 9, 12 phase 8).

What a judge does at the end of the demo: verify the chains, break one on
purpose and watch it go red at the exact index, put the two sides of a
disputed transaction next to each other, read the tool calls the agent chose,
and look at the measured revenue difference.

So these tests care about the properties that make it evidence rather than a
dashboard: the verification actually recomputes, the tamper is actually
caught, the dispute names the mismatch with indices, and the Revenue Lab
reports the costs alongside the wins.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.app import create_app as create_agent_app
from common import chainlog


@pytest.fixture
def audit(env) -> TestClient:
    return TestClient(create_agent_app(), raise_server_exceptions=True)


# -- chains and verification ------------------------------------------------

def test_a_clean_chain_verifies_green(audit):
    chainlog.append("buyer", "e", "something happened", {"n": 1})
    body = audit.get("/audit/verify").json()
    assert body["all_ok"] is True
    assert body["chains"]["buyer"]["ok"] is True
    assert body["chains"]["buyer"]["first_bad_index"] is None


def test_both_shops_and_the_buyer_are_covered(audit):
    body = audit.get("/audit/verify").json()
    assert set(body["chains"]) == {"buyer", "freshkart", "loomcraft"}


def test_every_entry_carries_a_human_readable_why(audit):
    chainlog.append("buyer", "payment_created", "all five wallet checks passed", {"a": 1})
    entries = audit.get("/audit/chains", params={"actor": "buyer"}).json()["chains"]["buyer"]
    assert entries
    for e in entries:
        assert e["why"].strip(), "a chain entry without a why is not evidence"


def test_tampering_breaks_the_chain_at_the_exact_index(audit):
    """The demo beat: edit history on disk and watch it go red where it was
    edited, not merely somewhere."""
    for i in range(6):
        chainlog.append("buyer", "e", f"entry {i}", {"n": i})

    result = audit.post("/audit/tamper", json={"actor": "buyer", "index": 3}).json()
    assert result["tampered_index"] == 3
    assert result["verifies"] is False
    assert result["first_bad_index"] == 3
    assert result["was"] != result["now"]

    # and the read-only verifier agrees, independently
    assert audit.get("/audit/verify").json()["chains"]["buyer"]["first_bad_index"] == 3


def test_a_tamper_is_caught_even_when_the_wording_barely_changes(audit):
    for i in range(4):
        chainlog.append("buyer", "e", f"entry {i}", {"n": i})
    audit.post("/audit/tamper", json={"actor": "buyer", "index": 1, "why": "entry 1 "})
    assert audit.get("/audit/verify").json()["all_ok"] is False


def test_tampering_needs_a_chain_worth_tampering_with(audit):
    assert audit.post("/audit/tamper", json={"actor": "buyer"}).status_code >= 400
    assert audit.post("/audit/tamper", json={"actor": "nobody"}).json()["code"] == "BAD_REQUEST"


# -- the dispute check ------------------------------------------------------

def test_a_dispute_reports_agreement_when_both_sides_match(audit):
    chainlog.append("buyer", "payment_created", "paid for txn_agree",
                    {"txn_ref": "txn_agree", "amount_paise": 50000})
    chainlog.append("freshkart", "payment_confirmed", "confirmed txn_agree",
                    {"txn_ref": "txn_agree", "charge_amount": 50000})

    body = audit.get("/audit/dispute/txn_agree").json()
    assert body["agreed"] is True
    assert body["shop_id"] == "freshkart"
    assert any("Both sides recorded" in f for f in body["findings"])


def test_a_dispute_names_the_mismatch_with_evidence_indices(audit):
    """Nobody has standardised what happens when two agents disagree about
    what was owed. This is the working answer, so it has to be specific."""
    chainlog.append("buyer", "approval_signed", "human approved txn_row at 50000 paise",
                    {"txn_ref": "txn_row", "amount_paise": 50000})
    chainlog.append("freshkart", "order_created", "order txn_row created",
                    {"txn_ref": "txn_row", "charge_amount": 61900})

    body = audit.get("/audit/dispute/txn_row").json()
    assert body["agreed"] is False
    finding = " ".join(body["findings"])
    assert "₹500.00" in finding and "₹619.00" in finding
    assert "₹119.00" in finding                      # the difference, named
    assert "buyer entry #" in finding and "entry #" in finding
    assert body["buyer"] and body["shop"]


def test_a_refusal_shows_up_in_the_dispute(audit):
    chainlog.append("buyer", "payment_refused", "shop charge differs from the approved amount",
                    {"txn_ref": "txn_ref_x", "amount_paise": 1000})
    body = audit.get("/audit/dispute/txn_ref_x").json()
    assert body["agreed"] is False
    assert any("records a refusal" in f for f in body["findings"])


def test_an_unknown_transaction_is_a_clean_404(audit):
    assert audit.get("/audit/dispute/txn_nothing").json()["code"] == "NOT_FOUND"


# -- the trace tab (spec 6.5) -----------------------------------------------

def test_traces_are_rebuilt_from_the_chain_not_from_memory(audit):
    """A trace that dies with the process is not evidence. These are read back
    out of the chain log, so they outlive the run that produced them."""
    chainlog.append("buyer", "agent_turn_started",
                    "shopper asked the agent at FreshKart: 'add 2 kg lemons'",
                    {"run_id": "run_t1", "shop_id": "freshkart"})
    chainlog.append("buyer", "agent_tool_call",
                    "search_catalog({}) -> 1 match; model's reason: find lemons",
                    {"run_id": "run_t1", "tool": "search_catalog", "args": {"query": "lemons"},
                     "ok": True, "latency_ms": 12})
    chainlog.append("buyer", "agent_tool_call",
                    "add_to_cart({}) -> cart 2 items; model's reason: add them",
                    {"run_id": "run_t1", "tool": "add_to_cart", "args": {"qty": 2},
                     "ok": True, "latency_ms": 20})

    turn = next(t for t in audit.get("/audit/traces").json()["turns"]
                if t["run_id"] == "run_t1")
    assert "add 2 kg lemons" in turn["asked"]
    assert [s["tool"] for s in turn["steps"]] == ["search_catalog", "add_to_cart"]
    assert all(s["why"] for s in turn["steps"])


def test_two_runs_of_the_same_sentence_can_differ(audit):
    """Spec 6.5: the artifact that proves it is not a script."""
    for run_id, tools_used in (("run_a", ["search_catalog"]),
                               ("run_b", ["search_catalog", "add_to_cart", "view_cart"])):
        chainlog.append("buyer", "agent_turn_started", "shopper asked the agent at X: 'the usual'",
                        {"run_id": run_id, "shop_id": "freshkart"})
        for t in tools_used:
            chainlog.append("buyer", "agent_tool_call", f"{t}() -> ok; model's reason: because",
                            {"run_id": run_id, "tool": t, "args": {}, "ok": True})

    turns = {t["run_id"]: t for t in audit.get("/audit/traces").json()["turns"]}
    assert len(turns["run_a"]["steps"]) != len(turns["run_b"]["steps"])


# -- the Revenue Lab (spec 9) -----------------------------------------------

def test_the_lab_reports_both_sides_and_the_lift(audit):
    body = audit.get("/audit/revenue-lab").json()
    assert body["goals"] == 20
    assert len(body["rows"]) == 20
    assert body["without"]["revenue"] > 0 and body["with_agent"]["revenue"] > 0
    assert body["lift_display"].startswith("₹")


def test_the_lab_shows_what_the_merchant_gives_away_not_only_what_it_gains(audit):
    """A scoreboard that counts only the wins is not evidence. The coupon
    money handed back has to be on it."""
    body = audit.get("/audit/revenue-lab").json()
    assert body["with_agent"]["discount"] > 0
    assert body["without"]["discount"] == 0        # unclaimed coupons cost nothing
    assert body["with_agent"]["coupon_orders"] > body["without"]["coupon_orders"]


def test_the_lift_is_attributed_to_rescued_sales(audit):
    """The honest reading: the gain comes from sales that would have been
    zero, not from coupons."""
    body = audit.get("/audit/revenue-lab").json()
    assert body["with_agent"]["rescued_orders"] > 0
    assert body["with_agent"]["rescued_revenue"] > 0
    # the lift is essentially the rescued revenue, not a coupon effect
    assert abs(body["lift_paise"] - body["with_agent"]["rescued_revenue"]) < body["lift_paise"] * 0.5


def test_the_lab_states_its_method_rather_than_implying_live_runs(audit):
    body = audit.get("/audit/revenue-lab").json()
    assert "not 20 live model conversations" in body["method"]
    assert "unaided" in body["assumptions"] and "assisted" in body["assumptions"]


def test_the_lab_is_repeatable(audit):
    a = audit.get("/audit/revenue-lab").json()
    b = audit.get("/audit/revenue-lab").json()
    assert a["with_agent"]["revenue"] == b["with_agent"]["revenue"]
    assert a["without"]["revenue"] == b["without"]["revenue"]
