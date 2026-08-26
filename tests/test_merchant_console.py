"""Phase 6, the merchant half (spec 6.2, 7.4): what the console reports.

The numbers here are the pitch. "The agent grew this merchant's revenue" is a
measured claim or it is nothing, so these tests care that the split is written
at purchase time rather than reconstructed, that a rescued sale is derived from
a reservation the shop actually refused, and that one merchant's console can
never reach the other's data.
"""
from __future__ import annotations

import pytest

from common import contact, mandate


def _buy(client, items, *, assisted=False, shopper_ref="shp_x", contact_text="",
         shop_id="freshkart", confirm=True):
    cart_id = client.post("/cart").json()["cart_id"]
    for item_id, variant, qty in items:
        client.patch(f"/cart/{cart_id}",
                     json={"op": "add", "item_id": item_id, "variant": variant, "qty": qty}
                     ).raise_for_status()
    token = mandate.issue(10_000_000, 5_000_000, [shop_id], ttl_seconds=600)
    order = client.post("/order",
                        json={"cart_id": cart_id, "shopper_ref": shopper_ref,
                              "contact": contact_text, "assisted": assisted},
                        headers={"Authorization": f"Mandate {token}"}).json()
    if confirm:
        client.post("/confirm-payment",
                    json={"txn_ref": order["txn_ref"], "razorpay_order_id": "order_test",
                          "payment_ref": "pay_test"}).raise_for_status()
    return order


# -- the measured claim -----------------------------------------------------

def test_an_empty_shop_reports_zeroes_not_errors(freshkart):
    s = freshkart.get("/merchant/summary").json()
    assert s["orders"] == 0 and s["revenue_paise"] == 0 and s["aov_paise"] == 0
    assert s["coupon_claim_rate"] == 0.0
    assert s["assisted"]["orders"] == 0 and s["unassisted"]["orders"] == 0
    assert s["rescued"]["orders"] == 0


def test_assisted_and_unassisted_are_counted_separately(freshkart):
    _buy(freshkart, [("lemons-1kg", "", 2)], assisted=False)          # 88.00
    _buy(freshkart, [("basmati-5kg", "", 1)], assisted=True)          # 429.00 - coupons

    s = freshkart.get("/merchant/summary").json()
    assert s["orders"] == 2
    assert s["assisted"]["orders"] == 1
    assert s["unassisted"]["orders"] == 1
    assert s["unassisted"]["revenue_paise"] == 8800
    assert s["assisted"]["revenue_paise"] == 33610          # FRESH50 + STAPLES10 applied
    assert s["revenue_paise"] == 8800 + 33610
    # and the AOV of each side is reported, which is where a lift shows up
    assert s["assisted"]["aov_paise"] == 33610
    assert s["unassisted"]["aov_paise"] == 8800


def test_only_paid_orders_count_as_revenue(freshkart):
    _buy(freshkart, [("lemons-1kg", "", 2)], confirm=False)   # quoted, never approved
    s = freshkart.get("/merchant/summary").json()
    assert s["orders"] == 0 and s["revenue_paise"] == 0


def test_coupon_claim_rate_is_a_real_ratio(freshkart):
    _buy(freshkart, [("lemons-1kg", "", 1)])        # 44.00, no coupon reaches it
    _buy(freshkart, [("basmati-5kg", "", 1)])       # 429.00, coupons apply
    s = freshkart.get("/merchant/summary").json()
    assert s["orders_with_coupon"] == 1
    assert s["coupon_claim_rate"] == 0.5


# -- rescued sales are derived, not asserted --------------------------------

def _out_of_stock_variant(client):
    for p in client.get("/catalog").json():
        for v in p.get("variants") or []:
            if v["stock"] == 0:
                return p["id"], v["label"]
    raise AssertionError("need an out-of-stock variant")


def test_a_sale_is_only_rescued_if_a_reservation_was_refused_first(loomcraft, monkeypatch):
    import shop.app as shop_app

    class Quiet:                       # :8003 not needed for this assertion
        @staticmethod
        def post(*a, **k):
            raise RuntimeError("agent offline")

    monkeypatch.setattr(shop_app, "httpx", Quiet)

    item_id, variant = _out_of_stock_variant(loomcraft)
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    loomcraft.post("/reserve",
                   json={"item_id": item_id, "variant": variant, "qty": 1,
                         "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"},
                   headers={"Authorization": f"Mandate {token}"}).raise_for_status()
    loomcraft.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 5})

    _buy(loomcraft, [(item_id, variant, 1)], shopper_ref="shp_zoya", shop_id="loomcraft")

    s = loomcraft.get("/merchant/summary").json()
    assert s["rescued"]["orders"] == 1
    assert s["rescued"]["revenue_paise"] > 0
    queue = loomcraft.get("/merchant/reservations").json()
    assert queue["reservations"][0]["status"] == "converted"


def test_an_ordinary_sale_is_not_counted_as_rescued(freshkart):
    _buy(freshkart, [("lemons-1kg", "", 2)])
    s = freshkart.get("/merchant/summary").json()
    assert s["rescued"]["orders"] == 0


def test_a_rescue_is_matched_across_the_shoppers_other_devices(loomcraft, monkeypatch):
    """They reserved on a laptop and bought on a phone. Still one rescue."""
    import shop.app as shop_app

    class Quiet:
        @staticmethod
        def post(*a, **k):
            raise RuntimeError("agent offline")

    monkeypatch.setattr(shop_app, "httpx", Quiet)

    item_id, variant = _out_of_stock_variant(loomcraft)
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    loomcraft.post("/reserve",
                   json={"item_id": item_id, "variant": variant, "qty": 1,
                         "contact_ref": "9821158848", "shopper_ref": "shp_laptop"},
                   headers={"Authorization": f"Mandate {token}"}).raise_for_status()
    loomcraft.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 5})

    _buy(loomcraft, [(item_id, variant, 1)], shopper_ref="shp_phone",
         contact_text="+91 98211 58848", shop_id="loomcraft")

    assert loomcraft.get("/merchant/summary").json()["rescued"]["orders"] == 1


# -- the cheat toggle (spec 6.1) --------------------------------------------

def test_cheat_mode_starts_off_and_toggles(freshkart):
    assert freshkart.get("/merchant/summary").json()["cheat_mode"] is False
    assert freshkart.post("/admin/cheat-mode", json={"on": True}).json()["cheat_mode"] is True
    assert freshkart.get("/merchant/summary").json()["cheat_mode"] is True
    assert freshkart.post("/admin/cheat-mode", json={"on": False}).json()["cheat_mode"] is False


def test_cheat_mode_quotes_honestly_then_demands_more(freshkart):
    """The villain has to lie AFTER the human agrees (spec 5.1: "between
    approval and charge"). Inflating the quote instead would just mean the
    shopper approved the higher number, and there would be nothing to catch -
    which is exactly what a first attempt at this did.

    The line items stay true, so the cart hash still matches and the lie has
    to be caught on the amount, which is what wallet check 4 compares.
    """
    freshkart.post("/admin/cheat-mode", json={"on": True})
    quoted = _buy(freshkart, [("lemons-1kg", "", 2)], confirm=False)
    assert quoted["charge_amount"] == 8800          # the shopper sees the true price

    demanded = freshkart.get(f"/order/{quoted['txn_ref']}").json()
    assert demanded["charge_amount"] > 8800         # the wallet is told another
    assert [(li["item_id"], li["qty"], li["unit_price_paise"])
            for li in demanded["line_items"]] == [("lemons-1kg", 2, 4400)]


def test_the_cheat_is_written_to_the_shops_own_chain(freshkart):
    from common import chainlog

    freshkart.post("/admin/cheat-mode", json={"on": True})
    order = _buy(freshkart, [("lemons-1kg", "", 1)], confirm=False)
    freshkart.get(f"/order/{order['txn_ref']}")
    entry = next(e for e in chainlog.tail("freshkart", 30) if e["event"] == "cheat_charge_issued")
    assert entry["data"]["demanded"] > entry["data"]["quoted"]


def test_the_wallet_refuses_a_shop_that_charges_more_than_was_approved(freshkart, monkeypatch):
    """The whole point of the toggle: an inflated charge must die at the
    wallet, not be discovered by the shopper on their statement."""
    from common import approval, wallet

    quoted = _buy(freshkart, [("lemons-1kg", "", 2)], confirm=False)
    freshkart.post("/admin/cheat-mode", json={"on": True})
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: freshkart.get(f"/order/{txn}").json())

    token = mandate.issue(10_000_000, 5_000_000, ["freshkart"], ttl_seconds=600)
    claims = mandate.verify(token)
    appr = approval.issue("freshkart", quoted["txn_ref"], quoted["line_items"],
                          quoted["charge_amount"], claims["jti"])

    with pytest.raises(Exception) as exc:
        wallet.pay(token, appr, "freshkart", quoted["charge_amount"], quoted["txn_ref"],
                   shop_url="http://testshop")
    assert "differs from the approved amount" in str(exc.value)
    assert freshkart.get(f"/order/{quoted['txn_ref']}").json()["status"] == "pending"


# -- one merchant, one console (spec 14) ------------------------------------

def test_a_console_reports_only_its_own_shops_numbers(freshkart, loomcraft):
    _buy(freshkart, [("lemons-1kg", "", 2)], shop_id="freshkart")
    _buy(loomcraft, [("tshirt-graphic-black", "M", 1)], shop_id="loomcraft")

    fresh = freshkart.get("/merchant/summary").json()
    loom = loomcraft.get("/merchant/summary").json()

    assert fresh["shop_id"] == "freshkart" and fresh["orders"] == 1
    assert loom["shop_id"] == "loomcraft" and loom["orders"] == 1
    assert fresh["revenue_paise"] != loom["revenue_paise"]
    # neither response carries any trace of the other merchant
    assert "loomcraft" not in str(fresh) and "freshkart" not in str(loom)


def test_restocking_needs_a_real_product_and_a_positive_quantity(freshkart):
    assert freshkart.post("/admin/restock",
                          json={"item_id": "lemons-1kg", "variant": "", "qty": 0}
                          ).json()["code"] == "BAD_REQUEST"
    assert freshkart.post("/admin/restock",
                          json={"item_id": "no-such-thing", "variant": "", "qty": 5}
                          ).json()["code"] == "NOT_FOUND"
