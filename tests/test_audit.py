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

def _pay(client, shop_id, items, assisted):
    """Place and confirm a real order, so the lab has something to measure."""
    from common import mandate

    cart = client.post("/cart").json()["cart_id"]
    for item_id, variant, qty in items:
        client.patch(f"/cart/{cart}",
                     json={"op": "add", "item_id": item_id, "variant": variant, "qty": qty}
                     ).raise_for_status()
    token = mandate.issue(10_000_000, 5_000_000, [shop_id], ttl_seconds=600)
    order = client.post("/order", json={"cart_id": cart, "assisted": assisted},
                        headers={"Authorization": f"Mandate {token}"}).json()
    client.post("/confirm-payment",
                json={"txn_ref": order["txn_ref"], "razorpay_order_id": "o",
                      "payment_ref": "p"}).raise_for_status()
    return order


def test_the_lab_measures_real_orders_rather_than_modelling_them(audit, freshkart):
    """An earlier version invented the comparison from assumed follow-through
    rates and printed it beside real rupees. Everything here has to come from
    orders that were actually placed."""
    _pay(freshkart, "freshkart", [("lemons-1kg", "", 2)], assisted=False)
    _pay(freshkart, "freshkart", [("basmati-5kg", "", 1)], assisted=True)

    body = audit.get("/audit/revenue-lab").json()
    assert body["source"] == "real paid orders from both shop databases"
    assert body["total_orders"] == 2
    assert body["unassisted"]["orders"] == 1
    assert body["assisted"]["orders"] == 1
    assert body["unassisted"]["revenue"] == 8800          # the real charge
    assert "goals" not in body and "lift_pct" not in body  # the modelled fields are gone


def test_an_empty_shop_reports_nothing_rather_than_a_projection(audit):
    body = audit.get("/audit/revenue-lab").json()
    assert body["total_orders"] == 0
    assert body["assisted"]["revenue"] == 0
    assert body["unassisted"]["revenue"] == 0
    assert body["comparable"] is False
    assert any("cannot be compared" in n for n in body["notes"])


def test_the_lab_refuses_to_compare_one_sided_data(audit, freshkart):
    _pay(freshkart, "freshkart", [("lemons-1kg", "", 1)], assisted=True)
    body = audit.get("/audit/revenue-lab").json()
    assert body["comparable"] is False
    assert body["aov_delta_paise"] == 0
    assert any("No unassisted orders" in n for n in body["notes"])


def test_the_lab_says_when_the_sample_is_too_small_to_generalise(audit, freshkart):
    _pay(freshkart, "freshkart", [("lemons-1kg", "", 1)], assisted=False)
    body = audit.get("/audit/revenue-lab").json()
    assert any("tells you what happened, not what will happen" in n for n in body["notes"])


def test_coupons_are_reported_as_a_cost_not_a_gain(audit, freshkart):
    _pay(freshkart, "freshkart", [("basmati-5kg", "", 1)], assisted=True)
    body = audit.get("/audit/revenue-lab").json()
    assert body["assisted"]["discount"] > 0
    assert any("margin the merchant handed back" in n for n in body["notes"])
    # and the counterfactual is taken from the same order, not modelled
    assert (body["assisted"]["undiscounted"]
            == body["assisted"]["revenue"] + body["assisted"]["discount"])


def test_a_rescued_sale_is_counted_from_the_reservation(audit, loomcraft, monkeypatch):
    """Rescued revenue is derived from a reservation the shop really refused,
    not from anything the agent asserts about itself."""
    import shop.app as shop_app
    from common import mandate

    class Quiet:
        @staticmethod
        def post(*a, **k):
            raise RuntimeError("agent offline")

    monkeypatch.setattr(shop_app, "httpx", Quiet)

    item_id = variant = None
    for p in loomcraft.get("/catalog").json():
        for v in p.get("variants") or []:
            if v["stock"] == 0:
                item_id, variant = p["id"], v["label"]
    assert item_id

    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    loomcraft.post("/reserve",
                   json={"item_id": item_id, "variant": variant, "qty": 1,
                         "contact_ref": "z@example.com", "shopper_ref": "shp_z"},
                   headers={"Authorization": f"Mandate {token}"}).raise_for_status()
    loomcraft.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 3})

    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.patch(f"/cart/{cart}",
                    json={"op": "add", "item_id": item_id, "variant": variant, "qty": 1}
                    ).raise_for_status()
    order = loomcraft.post("/order", json={"cart_id": cart, "shopper_ref": "shp_z",
                                           "assisted": True},
                           headers={"Authorization": f"Mandate {token}"}).json()
    loomcraft.post("/confirm-payment",
                   json={"txn_ref": order["txn_ref"], "razorpay_order_id": "o",
                         "payment_ref": "p"}).raise_for_status()

    body = audit.get("/audit/revenue-lab").json()
    assert body["assisted"]["rescued_orders"] == 1
    assert body["assisted"]["rescued_revenue"] > 0
    assert any("would otherwise have been zero" in n for n in body["notes"])


def test_the_lab_is_repeatable(audit, freshkart):
    _pay(freshkart, "freshkart", [("lemons-1kg", "", 1)], assisted=True)
    a = audit.get("/audit/revenue-lab").json()
    b = audit.get("/audit/revenue-lab").json()
    assert a["assisted"]["revenue"] == b["assisted"]["revenue"]


# -- phase 13: the trace reads as sentences ----------------------------------

def test_every_registered_tool_has_a_plain_sentence():
    """The Trace tab's evidence is worthless to the person it must convince if
    they cannot read it. Every tool the model can call must translate, and a
    tool this map has never met must fall back to the technical call rather
    than crash or lie."""
    from agent import tools

    for name in tools.REGISTRY:
        sentence = tools.plain_display(name, {"item_id": "toor-dal-1kg", "qty": 2,
                                              "query": "dal", "line_id": "line_x"})
        assert sentence and "(" not in sentence.split(" ")[0], (
            f"{name} renders as a function call, not a sentence: {sentence!r}")
        assert "line_625a" not in sentence and "item_id=" not in sentence

    unknown = tools.plain_display("brand_new_tool", {"x": 1})
    assert unknown.startswith("brand_new_tool(")


def test_plain_sentences_humanize_ids_not_expose_them():
    from agent import tools

    s = tools.plain_display("add_to_cart", {"item_id": "toor-dal-1kg", "qty": 2})
    assert "toor dal 1kg" in s and "toor-dal-1kg" not in s
    s = tools.plain_display("update_qty", {"line_id": "line_625a4a05", "qty": 1})
    assert "625a" not in s
