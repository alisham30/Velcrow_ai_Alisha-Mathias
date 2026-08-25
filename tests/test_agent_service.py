"""Trust-core service (:8003, Phase 2 form): mandate issue + approve-and-pay.
The wallet's Razorpay client is stubbed; the shop runs as a TestClient."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from common import chainlog, mandate, wallet


class FakeRazorpayClient:
    created: list[dict[str, Any]] = []

    def __init__(self, auth: tuple[str, str]) -> None:
        self.order = self

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        FakeRazorpayClient.created.append(payload)
        return {"id": f"order_FAKE{len(FakeRazorpayClient.created):03d}", **payload}


@pytest.fixture
def trust(env, freshkart, monkeypatch) -> TestClient:
    import agent.app as agent_app

    FakeRazorpayClient.created = []
    monkeypatch.setattr(wallet, "razorpay", SimpleNamespace(Client=FakeRazorpayClient))
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: freshkart.get(f"/order/{txn}").json())
    monkeypatch.setattr(agent_app, "_post_confirm",
                        lambda url, payload: freshkart.post("/confirm-payment", json=payload).json())
    return TestClient(agent_app.create_app())


def _browser_order(freshkart, token: str) -> dict[str, Any]:
    cart_id = freshkart.post("/cart").json()["cart_id"]
    for item, qty in (("lemons-1kg", 2), ("honey-500g", 2)):
        freshkart.patch(f"/cart/{cart_id}", json={"op": "add", "item_id": item, "qty": qty})
    return freshkart.post("/order", json={"cart_id": cart_id},
                          headers={"Authorization": f"Mandate {token}"}).json()


def test_mandate_endpoint_issues_and_logs(trust):
    r = trust.post("/mandate", json={"shops": ["freshkart"]})
    assert r.status_code == 201
    body = r.json()
    claims = mandate.verify(body["token"])
    assert claims["jti"] == body["jti"] and claims["shops"] == ["freshkart"]
    assert body["max_total_paise"] == 500000 and body["max_per_txn_paise"] == 300000
    assert chainlog.tail("buyer", 1)[0]["event"] == "mandate_issued"


def test_mandate_endpoint_rejects_unknown_shop(trust):
    r = trust.post("/mandate", json={"shops": ["evilmart"]})
    assert r.status_code == 422 and r.json()["code"] == "BAD_REQUEST"


def test_pay_happy_path_signs_approval_and_confirms(trust, freshkart):
    token = trust.post("/mandate", json={"shops": ["freshkart"]}).json()["token"]
    order = _browser_order(freshkart, token)
    r = trust.post("/pay", json={
        "shop_id": "freshkart", "shop_url": "test", "txn_ref": order["txn_ref"],
        "mandate_token": token, "approved_amount_paise": order["charge_amount"],
        "approved_items": order["line_items"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed"] is True
    assert body["razorpay_order_id"].startswith("order_FAKE")
    assert freshkart.get(f"/order/{order['txn_ref']}").json()["status"] == "paid"
    events = [e["event"] for e in chainlog.tail("buyer", 10)]
    assert "approval_signed" in events and "payment_created" in events


def test_pay_refuses_when_human_approved_a_different_amount(trust, freshkart):
    """The shop's charge and what the human saw must match — check 4."""
    token = trust.post("/mandate", json={"shops": ["freshkart"]}).json()["token"]
    order = _browser_order(freshkart, token)
    r = trust.post("/pay", json={
        "shop_id": "freshkart", "shop_url": "test", "txn_ref": order["txn_ref"],
        "mandate_token": token, "approved_amount_paise": order["charge_amount"] - 100,
        "approved_items": order["line_items"]})
    assert r.status_code == 409 and r.json()["code"] == "PRICE_CHANGED"
    assert FakeRazorpayClient.created == []
    assert freshkart.get(f"/order/{order['txn_ref']}").json()["status"] == "pending"


def test_pay_refuses_over_cap_mandate(trust, freshkart, buyer_mandate):
    order = _browser_order(freshkart, buyer_mandate)
    tiny = trust.post("/mandate", json={"shops": ["freshkart"], "max_total_paise": 1000,
                                        "max_per_txn_paise": 1000}).json()["token"]
    r = trust.post("/pay", json={
        "shop_id": "freshkart", "shop_url": "test", "txn_ref": order["txn_ref"],
        "mandate_token": tiny, "approved_amount_paise": order["charge_amount"],
        "approved_items": order["line_items"]})
    assert r.status_code == 402 and r.json()["code"] == "OVER_CAP"
    assert FakeRazorpayClient.created == []


def test_pay_requires_all_fields(trust):
    r = trust.post("/pay", json={"shop_id": "freshkart"})
    assert r.status_code == 422 and r.json()["code"] == "BAD_REQUEST"
