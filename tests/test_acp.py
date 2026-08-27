"""Phase 10: the ACP checkout adapter (spec 6.7).

The claim under test is narrow and checkable: this merchant exposes the
Agentic Commerce Protocol's checkout-session surface, version 2026-04-17, as
read from the live published spec - and that surface is a DIALECT of the
native one, not a second door. Same coupon engine, same stock arithmetic,
same mandate gate, same settlement. If the two protocols could ever quote
different totals for one basket, or one of them could move money the other
could not, the adapter would be a hole rather than a feature.

Shapes asserted here (statuses, message codes, Item-without-quantity,
LineItem, totals types, the {type, code, message} error) come from the live
spec and its JSON Schema, fetched 2026-08-27.
"""
from __future__ import annotations

import uuid

from common import mandate


def _mandate(shop_id: str, per_txn: int = 5_000_000) -> str:
    return mandate.issue(10_000_000, per_txn, [shop_id], ttl_seconds=600)


def _create(client, items, **extra):
    return client.post("/checkout_sessions",
                       json={"line_items": items, **extra},
                       headers={"Idempotency-Key": uuid.uuid4().hex})


def _complete(client, session_id, shop_id, token=None, idem=None):
    token = token or _mandate(shop_id)
    return client.post(f"/checkout_sessions/{session_id}/complete",
                       json={"buyer": {"email": "acp-buyer@example.com"},
                             "payment_data": {"handler_id": "razorpay_test_spt",
                                              "instrument": {"type": "card",
                                                             "credential": {"type": "spt",
                                                                            "token": "spt_test_1"}}}},
                       headers={"Authorization": f"Bearer {token}",
                                "Idempotency-Key": idem or uuid.uuid4().hex})


# -- the session speaks the spec's shapes ------------------------------------

def test_create_prices_a_session_in_spec_shape(freshkart):
    r = _create(freshkart, [{"id": "lemons-1kg"}])
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["id"].startswith("acp_cs_")
    assert s["status"] == "ready_for_payment"
    assert s["currency"] == "inr"
    line = s["line_items"][0]
    assert line["item"]["id"] == "lemons-1kg" and line["quantity"] == 1
    assert line["item"]["unit_amount"] == 4400
    kinds = [t["type"] for t in s["totals"]]
    assert kinds[0] == "items_base_amount" and kinds[-1] == "total"
    assert s["messages"] == []


def test_quantity_is_repetition_and_the_merchant_consolidates(freshkart):
    """The live 2026-04-17 spec's request Item has NO quantity field - three
    entries of one id are three units, consolidated into one LineItem."""
    s = _create(freshkart, [{"id": "lemons-1kg"}] * 3).json()
    assert len(s["line_items"]) == 1
    assert s["line_items"][0]["quantity"] == 3
    base = next(t for t in s["totals"] if t["type"] == "items_base_amount")
    assert base["amount"] == 3 * 4400


def test_variants_ride_in_the_documented_item_id_format(loomcraft):
    s = _create(loomcraft, [{"id": "kurti-indigo-cotton::M"}]).json()
    assert s["status"] == "ready_for_payment"
    assert s["line_items"][0]["item"]["id"] == "kurti-indigo-cotton::M"


def test_the_two_protocols_quote_the_same_total(freshkart):
    """One basket, two dialects, one price. The ACP total must equal what the
    native surface would charge - same subtotal, same coupon engine."""
    s = _create(freshkart, [{"id": "toor-dal-1kg"}, {"id": "honey-500g"}]).json()
    acp_total = next(t["amount"] for t in s["totals"] if t["type"] == "total")

    token = _mandate("freshkart")
    cart = freshkart.post("/cart").json()["cart_id"]
    for item in ("toor-dal-1kg", "honey-500g"):
        freshkart.post(f"/cart/{cart}/fulfil", headers={"Authorization": f"Mandate {token}"},
                       json={"item_id": item, "variant": "", "qty": 1, "mode": "add"})
    native = freshkart.post("/order", headers={"Authorization": f"Mandate {token}",
                                               "Idempotency-Key": uuid.uuid4().hex},
                            json={"cart_id": cart}).json()
    assert acp_total == native["charge_amount"]


# -- problems are spec messages, not bespoke errors --------------------------

def test_out_of_stock_is_the_specs_message_code(freshkart):
    s = _create(freshkart, [{"id": "ghee-500ml"}]).json()      # seeded at 0 stock
    assert s["status"] == "not_ready_for_payment"
    msg = s["messages"][0]
    assert msg["type"] == "error" and msg["code"] == "out_of_stock"
    assert msg["content_type"] == "plain" and msg["param"] == "$.line_items[0]"


def test_unknown_item_is_invalid_not_a_500(freshkart):
    s = _create(freshkart, [{"id": "no-such-thing"}]).json()
    assert s["status"] == "not_ready_for_payment"
    assert s["messages"][0]["code"] == "invalid"


def test_update_replaces_all_line_items(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    s2 = freshkart.post(f"/checkout_sessions/{s['id']}",
                        json={"line_items": [{"id": "atta-5kg"}, {"id": "atta-5kg"}]}).json()
    assert [(l["item"]["id"], l["quantity"]) for l in s2["line_items"]] == [("atta-5kg", 2)]


def test_transport_errors_use_the_specs_error_shape(freshkart):
    r = freshkart.get("/checkout_sessions/acp_cs_nope")
    assert r.status_code == 404
    body = r.json()
    assert set(body) == {"type", "code", "message"} and body["code"] == "not_found"


# -- money still has exactly one door ----------------------------------------

def test_complete_without_a_mandate_is_refused(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    r = freshkart.post(f"/checkout_sessions/{s['id']}/complete",
                       json={"buyer": {"email": "x@example.com"},
                             "payment_data": {"instrument": {"credential": {
                                 "type": "spt", "token": "spt_x"}}}},
                       headers={"Idempotency-Key": uuid.uuid4().hex})
    assert r.status_code == 401
    assert r.json()["code"] == "mandate_invalid"


def test_a_mandate_cap_binds_on_the_acp_surface_too(loomcraft):
    """The ACP dialect passes through the same OVER_CAP check in code. A cap
    below the basket price refuses the completion; nothing is charged."""
    s = _create(loomcraft, [{"id": "saree-linen-sage"}]).json()   # Rs 3,499
    small = mandate.issue(10_000_000, 100_000, ["loomcraft"], ttl_seconds=600)
    r = _complete(loomcraft, s["id"], "loomcraft", token=small)
    assert r.status_code == 402
    assert r.json()["code"] == "over_cap"
    assert loomcraft.get(f"/checkout_sessions/{s['id']}").json()["status"] != "completed"


def test_complete_places_and_settles_a_real_order(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}, {"id": "lemons-1kg"}]).json()
    r = _complete(freshkart, s["id"], "freshkart")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed"
    order = done["order"]
    assert order["type"] == "order" and order["checkout_session_id"] == s["id"]

    # The merchant's own record agrees: paid, through the same order path.
    native = freshkart.get(f"/order/{order['id']}").json()
    assert native["status"] == "paid"
    total = next(t["amount"] for t in done["totals"] if t["type"] == "total")
    assert native["charge_amount"] == total


def test_completing_twice_replays_not_double_charges(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    token = _mandate("freshkart")
    first = _complete(freshkart, s["id"], "freshkart", token=token, idem="acp-same-key")
    again = _complete(freshkart, s["id"], "freshkart", token=token, idem="acp-same-key")
    assert first.status_code == 200
    assert again.status_code == 200
    assert again.headers.get("idempotent-replay") == "true"
    assert again.json()["order"]["id"] == first.json()["order"]["id"]


def test_cancel_ends_the_session_and_charges_nothing(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    c = freshkart.post(f"/checkout_sessions/{s['id']}/cancel").json()
    assert c["status"] == "canceled"
    r = _complete(freshkart, s["id"], "freshkart")
    assert r.status_code == 409 and r.json()["code"] == "conflict"


def test_a_completed_session_cannot_be_reopened(freshkart):
    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    assert _complete(freshkart, s["id"], "freshkart").status_code == 200
    r = freshkart.post(f"/checkout_sessions/{s['id']}",
                       json={"line_items": [{"id": "atta-5kg"}]})
    assert r.status_code == 409
    assert freshkart.post(f"/checkout_sessions/{s['id']}/cancel").status_code == 409


# -- the manifest declares it, and the audit trail records it -----------------

def test_the_manifest_declares_the_checkout_block(freshkart, loomcraft):
    for client in (freshkart, loomcraft):
        m = client.get("/.well-known/agent-commerce.json").json()
        ck = m["checkout"]
        assert ck["protocol"] == "acp" and ck["version"] == "2026-04-17"
        assert ck["endpoints"]["create"] == "POST /checkout_sessions"
        # The claim stays honest: surface only, not full-protocol compliance.
        assert "not implemented" in ck["payment"]["note"]


def test_acp_activity_lands_on_the_chain(freshkart):
    from common import chainlog

    s = _create(freshkart, [{"id": "lemons-1kg"}]).json()
    _complete(freshkart, s["id"], "freshkart")
    events = [e["event"] for e in chainlog.tail("freshkart", 30)]
    assert "acp_session_created" in events
    assert "acp_session_completed" in events
