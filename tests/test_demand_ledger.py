"""Reserve flow records lost demand with its value (spec 6.1, 7.2)."""
from __future__ import annotations

from common import chainlog


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Mandate {token}"}


def _reserve(client, token, item="kurti-indigo-cotton", variant="S", contact="shopper-77", **extra):
    return client.post("/reserve", headers=_auth(token),
                       json={"item_id": item, "variant": variant, "contact_ref": contact, **extra})


def test_reserve_returns_pricing_and_restock(loomcraft, buyer_mandate):
    r = _reserve(loomcraft, buyer_mandate, qty=2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["res_id"].startswith("res_")
    assert body["product_name"] == "Indigo Cotton Kurti"
    assert body["qty"] == 2
    assert body["unit_price_paise"] == 149900
    assert body["lost_value_paise"] == 2 * 149900
    assert body["restock_date"] == "2026-08-29"
    assert body["status"] == "open"


def test_reserve_defaults_to_one_unit(loomcraft, buyer_mandate):
    body = _reserve(loomcraft, buyer_mandate).json()
    assert body["qty"] == 1 and body["lost_value_paise"] == 149900


def test_reserve_rejects_non_positive_qty(loomcraft, buyer_mandate):
    r = _reserve(loomcraft, buyer_mandate, qty=0)
    assert r.status_code == 422 and r.json()["code"] == "BAD_REQUEST"


def test_demand_ledger_aggregates_by_item_variant(loomcraft, buyer_mandate):
    _reserve(loomcraft, buyer_mandate, qty=2, contact="a@example.com")
    _reserve(loomcraft, buyer_mandate, qty=1, contact="b@example.com")
    _reserve(loomcraft, buyer_mandate, item="hoodie-fleece-grey", variant="S", contact="c@example.com")
    ledger = loomcraft.get("/merchant/demand-ledger").json()
    assert ledger["shop_id"] == "loomcraft" and ledger["currency"] == "INR"
    assert ledger["total_lost_value_paise"] == 3 * 149900 + 179900

    kurti = next(r for r in ledger["rows"] if r["item_id"] == "kurti-indigo-cotton")
    assert kurti["variant"] == "S"
    assert kurti["lost_units"] == 3
    assert kurti["lost_value_paise"] == 3 * 149900
    assert kurti["events"] == 2
    assert kurti["reason"] == "out_of_stock_reserved"
    assert kurti["in_stock"] == 0
    assert kurti["restock_date"] == "2026-08-29"
    assert len(kurti["reservation_ids"]) == 2
    assert {res["contact_ref"] for res in kurti["reservations"]} == {"a@example.com", "b@example.com"}
    # highest-value loss is listed first, so a merchant sees the worst gap on top
    assert ledger["rows"][0]["item_id"] == "kurti-indigo-cotton"


def test_demand_ledger_empty_before_any_reservation(loomcraft):
    ledger = loomcraft.get("/merchant/demand-ledger").json()
    assert ledger["rows"] == [] and ledger["total_lost_value_paise"] == 0


def test_reservation_is_chain_logged_with_value(loomcraft, buyer_mandate):
    _reserve(loomcraft, buyer_mandate, qty=2)
    entry = chainlog.tail("loomcraft", 1)[0]
    assert entry["event"] == "reservation_created"
    assert entry["data"]["lost_value_paise"] == 2 * 149900
    assert entry["data"]["qty"] == 2
    assert "demand recorded as lost" in entry["why"]
    ok, _ = chainlog.verify_chain("loomcraft")
    assert ok


def test_freshkart_records_no_demand_because_it_cannot_reserve(freshkart, buyer_mandate):
    r = freshkart.post("/reserve", headers=_auth(buyer_mandate),
                       json={"item_id": "ghee-500ml", "variant": "", "contact_ref": "x"})
    assert r.status_code == 400 and r.json()["code"] == "CAPABILITY_UNSUPPORTED"
    assert freshkart.get("/merchant/demand-ledger").json()["rows"] == []
