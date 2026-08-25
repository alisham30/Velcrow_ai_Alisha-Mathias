from __future__ import annotations

from typing import Any

from common import chainlog, mandate


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Mandate {token}"}


def _cart_with(client, items: list[tuple[str, str, int]]) -> str:
    cart_id = client.post("/cart").json()["cart_id"]
    for item_id, variant, qty in items:
        r = client.patch(f"/cart/{cart_id}",
                         json={"op": "add", "item_id": item_id, "variant": variant, "qty": qty})
        assert r.status_code == 200, r.text
    return cart_id


# -- catalog ----------------------------------------------------------------

def test_catalog_strips_cost_price(freshkart, loomcraft):
    for client in (freshkart, loomcraft):
        for route in ("/catalog", "/agent/catalog"):
            products = client.get(route).json()
            assert products, route
            for p in products:
                assert "cost_price_paise" not in p
                assert isinstance(p["price_paise"], int)


def test_product_detail_and_404(freshkart):
    p = freshkart.get("/product/lemons-1kg").json()
    assert p["name"] == "Lemons (1 kg)" and p["stock"] == 30
    r = freshkart.get("/product/nope")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"


# -- cart -------------------------------------------------------------------

def test_cart_quantities_add_update_remove(freshkart):
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    view = freshkart.get(f"/cart/{cart_id}").json()
    assert view["items"][0]["qty"] == 2 and view["subtotal_paise"] == 8800
    # adding the same item merges into the line
    freshkart.patch(f"/cart/{cart_id}", json={"op": "add", "item_id": "lemons-1kg", "qty": 1})
    view = freshkart.get(f"/cart/{cart_id}").json()
    assert len(view["items"]) == 1 and view["items"][0]["qty"] == 3
    line_id = view["items"][0]["line_id"]
    view = freshkart.patch(f"/cart/{cart_id}", json={"op": "update", "line_id": line_id, "qty": 5}).json()
    assert view["subtotal_paise"] == 5 * 4400
    view = freshkart.patch(f"/cart/{cart_id}", json={"op": "remove", "line_id": line_id}).json()
    assert view["items"] == [] and view["subtotal_paise"] == 0


def test_out_of_stock_typed_error_with_actions(freshkart, loomcraft):
    cart_id = freshkart.post("/cart").json()["cart_id"]
    r = freshkart.patch(f"/cart/{cart_id}", json={"op": "add", "item_id": "ghee-500ml", "qty": 1})
    body = r.json()
    assert r.status_code == 409 and body["code"] == "OUT_OF_STOCK"
    assert body["restock_date"] == "2026-08-28"
    assert body["available_actions"] == ["SELECT_ALTERNATIVE"]  # freshkart: no reservations
    cart_id = loomcraft.post("/cart").json()["cart_id"]
    r = loomcraft.patch(f"/cart/{cart_id}",
                        json={"op": "add", "item_id": "kurti-indigo-cotton", "variant": "S", "qty": 1})
    body = r.json()
    assert r.status_code == 409 and body["code"] == "OUT_OF_STOCK"
    assert body["restock_date"] == "2026-08-29"
    assert "RESERVE" in body["available_actions"]  # loomcraft reserves


def test_add_more_than_stock_refused(freshkart):
    cart_id = freshkart.post("/cart").json()["cart_id"]
    r = freshkart.patch(f"/cart/{cart_id}", json={"op": "add", "item_id": "infant-formula-400g", "qty": 7})
    assert r.status_code == 409 and r.json()["in_stock"] == 6


# -- coupons ----------------------------------------------------------------

def test_coupon_endpoint_reports_best_and_near_miss(freshkart):
    cart_id = _cart_with(freshkart, [("atta-5kg", "", 4)])  # staples 110000
    r = freshkart.post(f"/cart/{cart_id}/coupons").json()
    assert sorted(r["best"]["codes"]) == ["FRESH50", "STAPLES10"]
    assert r["best"]["discount_paise"] == 5000 + 11000
    assert r["subtotal_paise"] == 110000


def test_coupon_ineligible_names_unmet_condition(freshkart):
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])  # 8800
    r = freshkart.post(f"/cart/{cart_id}/coupons", json={"code": "FRESH150"})
    body = r.json()
    assert r.status_code == 422 and body["code"] == "COUPON_INELIGIBLE"
    assert "below" in body["unmet_condition"]


# -- order + mandate verification ------------------------------------------

def test_order_requires_mandate(freshkart):
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    r = freshkart.post("/order", json={"cart_id": cart_id})
    assert r.status_code == 402 and r.json()["code"] == "MANDATE_INVALID"
    r = freshkart.post("/order", json={"cart_id": cart_id},
                       headers={"Authorization": "Mandate not.a.jwt"})
    assert r.status_code == 402 and r.json()["code"] == "MANDATE_INVALID"
    assert chainlog.tail("freshkart", 1)[0]["event"] == "mandate_rejected"


def test_order_refused_for_unpermitted_shop(freshkart):
    loom_only = mandate.issue(1_000_000, 1_000_000, ["loomcraft"])
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    r = freshkart.post("/order", json={"cart_id": cart_id}, headers=_auth(loom_only))
    assert r.status_code == 403 and r.json()["code"] == "SHOP_NOT_PERMITTED"


def test_order_over_per_txn_cap_refused_shop_side(freshkart):
    tight = mandate.issue(1_000_000, 5000, ["freshkart"])  # Rs 50 per txn
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])  # 8800
    r = freshkart.post("/order", json={"cart_id": cart_id}, headers=_auth(tight))
    assert r.status_code == 402 and r.json()["code"] == "OVER_CAP"
    assert chainlog.tail("freshkart", 1)[0]["event"] == "order_refused"


def test_order_applies_best_coupon_and_holds_stock(freshkart, buyer_mandate):
    cart_id = _cart_with(freshkart, [("atta-5kg", "", 4)])  # 110000 staples
    r = freshkart.post("/order", json={"cart_id": cart_id}, headers=_auth(buyer_mandate))
    order = r.json()
    assert r.status_code == 201
    assert order["charge_amount"] == 110000 - 16000  # FRESH50 + STAPLES10 (10% of 110000, cap 120)
    assert order["shop_id"] == "freshkart" and order["currency"] == "INR"
    assert freshkart.get("/product/atta-5kg").json()["stock"] == 18 - 4  # stock held
    fetched = freshkart.get(f"/order/{order['txn_ref']}").json()
    assert fetched["status"] == "pending"
    assert fetched["line_items"] == order["line_items"]


def test_idempotency_key_returns_original_result(freshkart, buyer_mandate):
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    headers = {**_auth(buyer_mandate), "Idempotency-Key": "idem-abc"}
    r1 = freshkart.post("/order", json={"cart_id": cart_id}, headers=headers)
    r2 = freshkart.post("/order", json={"cart_id": cart_id}, headers=headers)
    assert r1.json()["txn_ref"] == r2.json()["txn_ref"]
    assert r2.headers.get("idempotent-replay") == "true"
    assert freshkart.get("/product/lemons-1kg").json()["stock"] == 30 - 2  # held once, not twice


def test_idempotency_key_with_different_payload_refused(freshkart, buyer_mandate):
    c1 = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    c2 = _cart_with(freshkart, [("honey-500g", "", 1)])
    headers = {**_auth(buyer_mandate), "Idempotency-Key": "idem-xyz"}
    assert freshkart.post("/order", json={"cart_id": c1}, headers=headers).status_code == 201
    r = freshkart.post("/order", json={"cart_id": c2}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENT_REPLAY"


def test_confirm_payment_and_double_confirm(freshkart, buyer_mandate):
    cart_id = _cart_with(freshkart, [("lemons-1kg", "", 2)])
    order = freshkart.post("/order", json={"cart_id": cart_id}, headers=_auth(buyer_mandate)).json()
    body = {"txn_ref": order["txn_ref"], "razorpay_order_id": "order_R1", "payment_ref": "pay_sim_1"}
    r = freshkart.post("/confirm-payment", json=body)
    assert r.status_code == 200 and r.json()["status"] == "paid"
    r2 = freshkart.post("/confirm-payment", json=body)
    assert r2.status_code == 409 and r2.json()["code"] == "IDEMPOTENT_REPLAY"
    # with an Idempotency-Key the original result is replayed instead
    cart_id = _cart_with(freshkart, [("honey-500g", "", 1)])
    order = freshkart.post("/order", json={"cart_id": cart_id}, headers=_auth(buyer_mandate)).json()
    body = {"txn_ref": order["txn_ref"], "razorpay_order_id": "order_R2", "payment_ref": "pay_sim_2"}
    headers = {"Idempotency-Key": "confirm-1"}
    assert freshkart.post("/confirm-payment", json=body, headers=headers).status_code == 200
    r3 = freshkart.post("/confirm-payment", json=body, headers=headers)
    assert r3.status_code == 200 and r3.headers.get("idempotent-replay") == "true"


# -- reservations -----------------------------------------------------------

def test_reserve_out_of_stock_variant_loomcraft(loomcraft, buyer_mandate):
    r = loomcraft.post("/reserve", headers=_auth(buyer_mandate),
                       json={"item_id": "kurti-indigo-cotton", "variant": "S",
                             "contact_ref": "shopper-77"})
    body = r.json()
    assert r.status_code == 201 and body["res_id"].startswith("res_")
    assert body["restock_date"] == "2026-08-29"
    assert chainlog.tail("loomcraft", 1)[0]["event"] == "reservation_created"


def test_reserve_requires_mandate(loomcraft):
    r = loomcraft.post("/reserve", json={"item_id": "kurti-indigo-cotton", "variant": "S",
                                         "contact_ref": "shopper-77"})
    assert r.status_code == 402 and r.json()["code"] == "MANDATE_INVALID"


def test_reserve_in_stock_item_refused(loomcraft, buyer_mandate):
    r = loomcraft.post("/reserve", headers=_auth(buyer_mandate),
                       json={"item_id": "kurti-indigo-cotton", "variant": "M",
                             "contact_ref": "shopper-77"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_OUT_OF_STOCK"


def test_reserve_unsupported_at_freshkart(freshkart, buyer_mandate):
    r = freshkart.post("/reserve", headers=_auth(buyer_mandate),
                       json={"item_id": "ghee-500ml", "variant": "", "contact_ref": "shopper-77"})
    assert r.status_code == 400 and r.json()["code"] == "CAPABILITY_UNSUPPORTED"


# -- agent-readable surface (spec 6.6) --------------------------------------

def test_manifest_shapes_differ_per_shop(freshkart, loomcraft):
    fk = freshkart.get("/.well-known/agent-commerce.json").json()
    lc = loomcraft.get("/.well-known/agent-commerce.json").json()
    for m in (fk, lc):
        assert m["catalog"] == "/agent/catalog"
        assert m["manifest_version"] == "velcrow-0.1"
        assert m["auth"]["presented_as"] == "Authorization: Mandate <jwt>"
        assert m["payment"] == {"provider": "razorpay", "mode": "test"}
        assert m["policies"]["price_lock_seconds"] == 300
    assert fk["merchant"] == {"id": "freshkart", "name": "FreshKart", "category": "grocery"}
    assert lc["merchant"] == {"id": "loomcraft", "name": "Loomcraft", "category": "apparel"}
    assert "reservations" in lc["capabilities"] and "reservations" not in fk["capabilities"]
    assert "restock_notify" in lc["capabilities"] and "restock_notify" not in fk["capabilities"]


def test_capability_negotiation_differs_per_shop(freshkart, loomcraft):
    ask: dict[str, Any] = {"capabilities": {"discounts": True, "reservations": True,
                                            "human_approval": True, "teleportation": True}}
    fk = freshkart.post("/agent/capabilities", json=ask).json()["capabilities"]
    lc = loomcraft.post("/agent/capabilities", json=ask).json()["capabilities"]
    assert fk["variants"] == "pack" and lc["variants"] == "size"
    assert fk["reservations"] is False and lc["reservations"] is True
    assert fk["restock_notify"] is False and lc["restock_notify"] is True
    assert fk["teleportation"] is False and lc["teleportation"] is False  # unknown -> false
    assert fk != lc


def test_agent_catalog_is_machine_readable(loomcraft):
    products = loomcraft.get("/agent/catalog").json()
    kurti = next(p for p in products if p["id"] == "kurti-indigo-cotton")
    s = next(v for v in kurti["variants"] if v["label"] == "S")
    assert s["stock"] == 0 and s["restock_date"] == "2026-08-29"
    assert isinstance(kurti["price_paise"], int)
    assert kurti["tags"] and isinstance(kurti["exact_only"], bool)
