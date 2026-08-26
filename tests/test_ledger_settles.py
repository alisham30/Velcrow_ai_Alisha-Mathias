"""Phase 9: the numbers stop lying about themselves (spec 16.9).

Two things were wrong and both made the system report money it was not losing:

1. The ledger never closed. A refusal that had been restocked, notified and
   BOUGHT went on being counted as demand still being lost, so the merchant
   console showed a hole that was already filled and the growth agent proposed
   spending cash to fix it.

2. A rescued sale was only ever derived from the reservations table. FreshKart
   takes no reservations - it can only tell you when something is back - so
   every sale it won back that way was invisible, and the shop that most needed
   the credit got none of it.

The tests below buy the refusal back and then check that both the ledger and
the order agree it happened.
"""
from __future__ import annotations

import uuid

from common import mandate


def _auth(shop_id: str) -> dict[str, str]:
    return {"Authorization": f"Mandate {mandate.issue(10_000_000, 5_000_000, [shop_id],
                                                      ttl_seconds=600)}"}


def _out_of_stock_flat(client) -> str:
    for p in client.get("/catalog").json():
        if not p.get("variants") and p.get("stock", 0) == 0:
            return p["id"]
    raise AssertionError("need a flat product that is out of stock")


def _buy(client, shop_id, cart, shopper, contact):
    order = client.post("/order", headers={**_auth(shop_id),
                                           "Idempotency-Key": uuid.uuid4().hex},
                        json={"cart_id": cart, "assisted": True,
                              "shopper_ref": shopper, "contact": contact})
    assert order.status_code == 201, order.text
    txn = order.json()["txn_ref"]
    paid = client.post("/confirm-payment",
                       json={"txn_ref": txn, "razorpay_order_id": "order_test",
                             "payment_ref": "pay_test"})
    assert paid.status_code == 200, paid.text
    return txn, paid.json()


def _refuse_then_restock(client, item_id, qty, shopper, contact):
    """The full arc: turned away, shop restocks, shopper comes back."""
    cart = client.post("/cart").json()["cart_id"]
    refused = client.post(f"/cart/{cart}/fulfil", headers=_auth("freshkart"),
                          json={"item_id": item_id, "variant": "", "qty": qty, "mode": "add",
                                "shopper_ref": shopper, "contact_ref": contact}).json()
    assert refused["added"] == 0 and refused["shortfall"] == qty
    client.post("/admin/restock", json={"item_id": item_id, "variant": "", "qty": qty + 2})
    return cart


# -- a shop that cannot hold a unit can still win the sale back ---------------

def test_a_shop_without_reservations_gets_credit_for_the_rescue(freshkart):
    item_id = _out_of_stock_flat(freshkart)
    shopper, contact = "shp_meera", "meera@example.com"
    _refuse_then_restock(freshkart, item_id, 2, shopper, contact)

    back = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{back}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 2, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _txn, body = _buy(freshkart, "freshkart", back, shopper, contact)

    # No reservation was ever taken - this shop cannot take one.
    assert body["rescued_reservations"] == []
    assert len(body["recovered_demand"]) == 1
    assert body["recovered_demand"][0]["item_id"] == item_id
    assert body["recovered_demand"][0]["qty"] == 2


def test_the_ledger_closes_the_row_it_recovered(freshkart):
    item_id = _out_of_stock_flat(freshkart)
    shopper, contact = "shp_ravi", "ravi@example.com"
    _refuse_then_restock(freshkart, item_id, 2, shopper, contact)

    before = freshkart.get("/merchant/demand-ledger").json()
    assert before["recovered_value_paise"] == 0

    back = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{back}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 2, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _buy(freshkart, "freshkart", back, shopper, contact)

    after = freshkart.get("/merchant/demand-ledger").json()
    row = next(r for r in after["rows"] if r["item_id"] == item_id)
    assert row["state"] == "recovered"
    assert row["recovered_units"] == 2
    assert row["outstanding_units"] == 0
    assert after["outstanding_value_paise"] == 0
    assert after["recovered_value_paise"] == before["total_lost_value_paise"]
    # History is kept: the refusal still happened, it is just no longer open.
    assert after["total_lost_value_paise"] == before["total_lost_value_paise"]


def test_only_what_the_basket_actually_supplies_is_settled(freshkart):
    """Buying 1 of something you were refused 4 of recovers one refusal, not
    the whole hole. The rest is still outstanding and still worth acting on."""
    item_id = _out_of_stock_flat(freshkart)
    shopper, contact = "shp_asha", "asha@example.com"
    cart = freshkart.post("/cart").json()["cart_id"]
    for _ in range(4):      # four separate refusals of one unit each
        freshkart.post(f"/cart/{cart}/fulfil", headers=_auth("freshkart"),
                       json={"item_id": item_id, "variant": "", "qty": 1, "mode": "add",
                             "shopper_ref": shopper, "contact_ref": contact})
    freshkart.post("/admin/restock", json={"item_id": item_id, "variant": "", "qty": 10})

    back = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{back}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 1, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _txn, body = _buy(freshkart, "freshkart", back, shopper, contact)

    assert len(body["recovered_demand"]) == 1
    row = next(r for r in freshkart.get("/merchant/demand-ledger").json()["rows"]
               if r["item_id"] == item_id)
    assert row["recovered_units"] == 1
    assert row["lost_units"] == 4


def test_a_stranger_buying_the_same_item_settles_nothing(freshkart):
    """The recovery is matched to the person who was refused. Otherwise any
    passing sale would paper over somebody else's lost demand."""
    item_id = _out_of_stock_flat(freshkart)
    _refuse_then_restock(freshkart, item_id, 2, "shp_nikhil", "nikhil@example.com")

    cart = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{cart}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 2, "mode": "add",
                         "shopper_ref": "shp_someone_else", "contact_ref": "else@example.com"})
    _txn, body = _buy(freshkart, "freshkart", cart, "shp_someone_else", "else@example.com")

    assert body["recovered_demand"] == []
    row = next(r for r in freshkart.get("/merchant/demand-ledger").json()["rows"]
               if r["item_id"] == item_id)
    assert row["recovered_units"] == 0


def test_a_recovered_refusal_is_not_notified_twice(freshkart):
    """Once they have bought, a later restock must not chase them about it."""
    item_id = _out_of_stock_flat(freshkart)
    shopper, contact = "shp_dev", "dev@example.com"
    cart = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{cart}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 1, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    freshkart.post("/admin/restock", json={"item_id": item_id, "variant": "", "qty": 5})

    back = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{back}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 1, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _buy(freshkart, "freshkart", back, shopper, contact)

    again = freshkart.post("/admin/restock",
                           json={"item_id": item_id, "variant": "", "qty": 5}).json()
    assert not any(n.get("contact_key") == contact or n.get("shopper_ref") == shopper
                   for n in again.get("notified", []))


# -- the growth agent must read the settled figure, not the historical one ----

def test_the_growth_agent_sees_the_same_outstanding_number(freshkart, monkeypatch):
    from agent import merchant

    item_id = _out_of_stock_flat(freshkart)
    shopper, contact = "shp_tara", "tara@example.com"
    _refuse_then_restock(freshkart, item_id, 2, shopper, contact)
    back = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{back}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": item_id, "variant": "", "qty": 2, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _buy(freshkart, "freshkart", back, shopper, contact)

    monkeypatch.setattr(merchant, "_get",
                        lambda _url, path, **kw: freshkart.get(path, **kw).json())
    view = merchant.get_demand_ledger({"shop_url": "http://testshop", "shop_id": "freshkart"})

    ledger = freshkart.get("/merchant/demand-ledger").json()
    assert view["outstanding_display"].endswith("0.00")
    assert ledger["outstanding_value_paise"] == 0
    assert all(r["outstanding_units"] == 0 for r in view["rows"])
    assert "Act on OUTSTANDING only" in view["note"]


# -- the Revenue Lab reports what the agent did, and says what it did not -----

def test_the_lab_never_claims_the_coupon_as_an_agent_win():
    from lab import revenue_lab

    r = revenue_lab.run()
    names = {d["name"] for d in r["differentiators"]}
    assert "coupon claim rate" not in names
    assert names == {"average basket", "sales rescued from a stock-out"}
    # Repeat orders are off the list too: shopper identity is the shop's, and
    # the plain storefront gets it as well.
    assert "repeat orders" not in names
    assert any("NOT an agent metric" in n for n in r["notes"])


def test_the_lab_reads_only_paid_orders(freshkart):
    """An order that was quoted and never paid is not revenue, and must not
    reach the scoreboard."""
    from lab import revenue_lab

    cart = freshkart.post("/cart").json()["cart_id"]
    freshkart.post(f"/cart/{cart}/fulfil", headers=_auth("freshkart"),
                   json={"item_id": "lemons-1kg", "variant": "", "qty": 2, "mode": "add"})
    freshkart.post("/order", headers={**_auth("freshkart"), "Idempotency-Key": uuid.uuid4().hex},
                   json={"cart_id": cart, "assisted": True, "shopper_ref": "shp_ghost"})

    assert revenue_lab.run()["total_orders"] == 0


# -- the reservation path has to settle its ledger row as well ----------------

def test_a_reserved_refusal_that_is_bought_back_stops_reading_as_lapsed(loomcraft):
    """Loomcraft CAN hold a unit, and that was the path that stayed broken
    longest: the reservation closed, the order was flagged rescued, and the
    demand row behind it sat there as 'lapsed' - money written off that was
    already back in the till."""
    item_id, on_hand = "saree-linen-sage", 4
    shopper, contact = "shp_ira", "ira@example.com"

    cart = loomcraft.post("/cart").json()["cart_id"]
    refused = loomcraft.post(f"/cart/{cart}/fulfil", headers=_auth("loomcraft"),
                             json={"item_id": item_id, "variant": "", "qty": on_hand + 2,
                                   "mode": "add", "shopper_ref": shopper,
                                   "contact_ref": contact}).json()
    assert refused["reserved"] == 2, refused          # this shop does hold units
    loomcraft.post("/admin/restock", json={"item_id": item_id, "variant": "", "qty": 2})

    back = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{back}/fulfil", headers=_auth("loomcraft"),
                   json={"item_id": item_id, "variant": "", "qty": 2, "mode": "add",
                         "shopper_ref": shopper, "contact_ref": contact})
    _txn, body = _buy(loomcraft, "loomcraft", back, shopper, contact)

    assert len(body["rescued_reservations"]) == 1     # the reservation closed
    assert len(body["recovered_demand"]) == 1         # and so did the ledger row

    ledger = loomcraft.get("/merchant/demand-ledger").json()
    row = next(r for r in ledger["rows"] if r["item_id"] == item_id)
    assert row["state"] == "recovered"
    assert row["recovered_units"] == 2
    assert ledger["lapsed_value_paise"] == 0
    assert ledger["outstanding_value_paise"] == 0


def test_a_reservation_records_who_was_refused(loomcraft):
    """The ledger row must carry the shopper, or nothing downstream can match a
    later purchase to the refusal it answered."""
    loomcraft.post("/reserve", headers=_auth("loomcraft"),
                   json={"item_id": "kurti-indigo-cotton", "variant": "S", "qty": 1,
                         "contact_ref": "uma@example.com", "shopper_ref": "shp_uma"})
    row = next(r for r in loomcraft.get("/merchant/demand-ledger").json()["rows"]
               if r["item_id"] == "kurti-indigo-cotton")
    assert row["state"] == "outstanding"
    assert row["outstanding_units"] == 1
