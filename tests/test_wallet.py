from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from common import approval, chainlog, errors, mandate, wallet


class FakeRazorpayClient:
    """Stands in for razorpay.Client — records order.create calls."""

    created: list[dict[str, Any]] = []

    def __init__(self, auth: tuple[str, str]) -> None:
        self.auth = auth
        self.order = self

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        FakeRazorpayClient.created.append(payload)
        return {"id": f"order_FAKE{len(FakeRazorpayClient.created):03d}", **payload}


@pytest.fixture
def rzp(monkeypatch):
    FakeRazorpayClient.created = []
    monkeypatch.setattr(wallet, "razorpay", SimpleNamespace(Client=FakeRazorpayClient))
    return FakeRazorpayClient


@pytest.fixture
def shop(freshkart, monkeypatch):
    """freshkart TestClient wired into wallet's charge fetch."""
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: freshkart.get(f"/order/{txn}").json())
    return freshkart


def _place_order(shop, token: str, items: list[tuple[str, str, int]]) -> dict[str, Any]:
    cart = shop.post("/cart").json()
    for item_id, variant, qty in items:
        r = shop.patch(f"/cart/{cart['cart_id']}",
                       json={"op": "add", "item_id": item_id, "variant": variant, "qty": qty})
        assert r.status_code == 200, r.text
    r = shop.post("/order", json={"cart_id": cart["cart_id"]},
                  headers={"Authorization": f"Mandate {token}"})
    assert r.status_code == 201, r.text
    return r.json()


def _approve(order: dict[str, Any], jti: str, amount: int | None = None,
             items: list[dict[str, Any]] | None = None) -> str:
    return approval.issue("freshkart", order["txn_ref"],
                          items if items is not None else order["line_items"],
                          amount if amount is not None else order["charge_amount"], jti)


def test_happy_path_full_purchase(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 2), ("honey-500g", "", 2)])
    appr = _approve(order, jti)
    result = wallet.pay(buyer_mandate, appr, "freshkart", order["charge_amount"],
                        order["txn_ref"], shop_url="test")
    assert result["razorpay_order_id"].startswith("order_FAKE")
    assert len(rzp.created) == 1
    assert rzp.created[0]["amount"] == order["charge_amount"]
    assert rzp.created[0]["currency"] == "INR"
    assert mandate.spent(jti) == order["charge_amount"]  # budget consumed
    confirm = shop.post("/confirm-payment", json={
        "txn_ref": order["txn_ref"], "razorpay_order_id": result["razorpay_order_id"],
        "payment_ref": result["payment_ref"]})
    assert confirm.status_code == 200 and confirm.json()["status"] == "paid"
    assert chainlog.tail("buyer", 1)[0]["event"] == "payment_created"
    shop_events = [e["event"] for e in chainlog.tail("freshkart", 10)]
    assert "payment_created" in shop_events and "payment_confirmed" in shop_events
    ok, _ = chainlog.verify_chain("buyer")
    assert ok
    ok, _ = chainlog.verify_chain("freshkart")
    assert ok


def test_check1_forged_mandate_refused_and_logged_on_both_chains(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 1)])
    appr = _approve(order, jti)
    head, payload, sig = buyer_mandate.split(".")
    forged = f"{head}.{payload}.{'A' * len(sig)}"
    with pytest.raises(errors.MandateInvalid):
        wallet.pay(forged, appr, "freshkart", order["charge_amount"], order["txn_ref"], shop_url="t")
    assert rzp.created == []
    for actor in ("buyer", "freshkart"):
        last = chainlog.tail(actor, 1)[0]
        assert last["event"] == "payment_refused"
        assert last["data"]["code"] == "MANDATE_INVALID"


def test_check2_shop_not_permitted(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 1)])
    loom_only = mandate.issue(1_000_000, 1_000_000, ["loomcraft"])
    appr = _approve(order, jti)
    with pytest.raises(errors.ShopNotPermitted):
        wallet.pay(loom_only, appr, "freshkart", order["charge_amount"], order["txn_ref"], shop_url="t")
    assert rzp.created == []


def test_check3_per_txn_cap_refused(shop, rzp, buyer_mandate):
    order = _place_order(shop, buyer_mandate, [("basmati-5kg", "", 1)])  # 42900
    tight = mandate.issue(1_000_000, order["charge_amount"] - 1, ["freshkart"])
    tight_jti = mandate.verify(tight)["jti"]
    appr = approval.issue("freshkart", order["txn_ref"], order["line_items"],
                          order["charge_amount"], tight_jti)
    with pytest.raises(errors.OverCap):
        wallet.pay(tight, appr, "freshkart", order["charge_amount"], order["txn_ref"], shop_url="t")
    assert rzp.created == []
    assert mandate.spent(tight_jti) == 0
    assert chainlog.tail("buyer", 1)[0]["data"]["code"] == "OVER_CAP"


def test_check3_max_total_exhaustion_across_purchases(shop, rzp):
    # lemons 4400 x 2 = 8800/order; total budget covers one order only
    m = mandate.issue(10000, 9000, ["freshkart"])
    jti = mandate.verify(m)["jti"]
    o1 = _place_order(shop, m, [("lemons-1kg", "", 2)])
    wallet.pay(m, _approve(o1, jti), "freshkart", o1["charge_amount"], o1["txn_ref"], shop_url="t")
    o2 = _place_order(shop, m, [("lemons-1kg", "", 2)])
    with pytest.raises(errors.OverCap):
        wallet.pay(m, _approve(o2, jti), "freshkart", o2["charge_amount"], o2["txn_ref"], shop_url="t")
    assert len(rzp.created) == 1  # only the first purchase reached Razorpay


def test_check3_reservation_released_on_later_failure(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 2)])
    bad_amount = _approve(order, jti, amount=order["charge_amount"] + 100)
    with pytest.raises(errors.PriceChanged):
        wallet.pay(buyer_mandate, bad_amount, "freshkart", order["charge_amount"],
                   order["txn_ref"], shop_url="t")
    assert mandate.spent(jti) == 0  # reserve rolled back
    assert rzp.created == []


def test_check4_cart_hash_mismatch_names_the_line(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 2)])
    tampered = [dict(order["line_items"][0], qty=5)]  # approve a different basket
    appr = _approve(order, jti, items=tampered)
    with pytest.raises(errors.PriceChanged) as exc:
        wallet.pay(buyer_mandate, appr, "freshkart", order["charge_amount"],
                   order["txn_ref"], shop_url="t")
    assert "lemons-1kg" in exc.value.why  # the log names the changed line
    assert rzp.created == []
    assert mandate.spent(jti) == 0


def test_check4_approval_nonce_single_use(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 2)])
    appr = _approve(order, jti)
    wallet.pay(buyer_mandate, appr, "freshkart", order["charge_amount"], order["txn_ref"], shop_url="t")
    with pytest.raises(errors.MandateInvalid, match="nonce"):
        wallet.pay(buyer_mandate, appr, "freshkart", order["charge_amount"], order["txn_ref"], shop_url="t")
    assert len(rzp.created) == 1


def test_check4_paid_order_cannot_be_paid_again(shop, rzp, buyer_mandate):
    jti = mandate.verify(buyer_mandate)["jti"]
    order = _place_order(shop, buyer_mandate, [("lemons-1kg", "", 2)])
    shop.post("/confirm-payment", json={"txn_ref": order["txn_ref"],
                                        "razorpay_order_id": "order_X", "payment_ref": "pay_X"})
    appr = _approve(order, jti)
    with pytest.raises(errors.IdempotentReplay):
        wallet.pay(buyer_mandate, appr, "freshkart", order["charge_amount"],
                   order["txn_ref"], shop_url="t")
    assert rzp.created == []
