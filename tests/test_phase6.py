"""Phase 6 (spec 12): restock -> reservation callback -> one-tap comeback sale.

This is the first path in the system that starts with nobody at the keyboard.
The shopper was turned away, the merchant restocked later, and the agent goes
and finds them. So the tests care about two things in particular: that it
really does fire without a shopper present, and that starting unprompted buys
it no authority it did not already have.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.app import create_app as create_agent_app
from agent import runtime
from common import chainlog, mandate


@pytest.fixture
def agent_client(env) -> TestClient:
    runtime.OFFERS.clear()
    return TestClient(create_agent_app(), raise_server_exceptions=True)


@pytest.fixture
def loom(loomcraft, agent_client, monkeypatch):
    """Loomcraft with its restock callbacks pointed at the in-process agent."""
    import shop.app as shop_app

    class Routed:
        @staticmethod
        def post(url: str, json: dict[str, Any] | None = None, timeout: int = 0):
            assert url.endswith("/callback/restock")
            return agent_client.post("/callback/restock", json=json)

    monkeypatch.setattr(shop_app, "httpx", Routed)
    return loomcraft


def _reserve(loom, item_id: str, variant: str, shopper_ref: str = "shp_zoya") -> str:
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    resp = loom.post("/reserve",
                     json={"item_id": item_id, "variant": variant, "qty": 1,
                           "contact_ref": "zoya@example.com", "shopper_ref": shopper_ref},
                     headers={"Authorization": f"Mandate {token}"})
    resp.raise_for_status()
    return resp.json()["res_id"]


def _an_out_of_stock_variant(loom) -> tuple[str, str]:
    for p in loom.get("/catalog").json():
        for v in p.get("variants") or []:
            if v["stock"] == 0:
                return p["id"], v["label"]
    raise AssertionError("the apparel catalog needs an out-of-stock size for this test")


# -- the agent acts with nobody shopping ------------------------------------

def test_restocking_reaches_the_shopper_who_was_turned_away(loom, agent_client):
    item_id, variant = _an_out_of_stock_variant(loom)
    res_id = _reserve(loom, item_id, variant)

    # nobody is shopping; the merchant restocks
    result = loom.post("/admin/restock",
                       json={"item_id": item_id, "variant": variant, "qty": 20}).json()
    assert result["stock"] == 20
    assert result["reservations_notified"][0]["accepted"] is True

    # the agent is now holding something it was never asked for
    offers = agent_client.get("/agent/offers",
                              params={"shop": "apparel", "shopper_ref": "shp_zoya"}).json()["offers"]
    assert len(offers) == 1
    assert offers[0]["item_id"] == item_id
    assert offers[0]["variant"] == variant
    assert offers[0]["res_id"] == res_id
    assert offers[0]["line_total_display"].startswith("₹")


def test_the_reservation_is_marked_notified_not_left_open(loom):
    item_id, variant = _an_out_of_stock_variant(loom)
    res_id = _reserve(loom, item_id, variant)
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 5})

    queue = loom.get("/merchant/reservations").json()
    row = next(r for r in queue["reservations"] if r["res_id"] == res_id)
    assert row["status"] == "notified"
    assert queue["open_count"] == 0


def test_a_restock_with_nobody_waiting_contacts_nobody(loom, agent_client):
    """Not every restock is a rescue. Silence is a correct outcome."""
    item_id, variant = _an_out_of_stock_variant(loom)
    result = loom.post("/admin/restock",
                       json={"item_id": item_id, "variant": variant, "qty": 5}).json()
    assert result["reservations_notified"] == []
    assert agent_client.get("/agent/offers",
                            params={"shop": "apparel",
                                    "shopper_ref": "shp_zoya"}).json()["offers"] == []


def test_both_the_restock_and_the_offer_are_chain_logged(loom):
    item_id, variant = _an_out_of_stock_variant(loom)
    _reserve(loom, item_id, variant)
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 12})

    shop_events = [e["event"] for e in chainlog.tail("loomcraft", 40)]
    assert "restocked" in shop_events
    assert "restock_callback_sent" in shop_events

    offered = next(e for e in chainlog.tail("buyer", 40) if e["event"] == "comeback_offered")
    assert "is back at loomcraft" in offered["why"]
    assert "without the shopper's approval" in offered["why"]


# -- starting unprompted buys it no extra authority -------------------------

def test_a_revoked_mandate_means_the_agent_stays_silent(loom, agent_client):
    """Revocation is the shopper saying "stop acting for me". A restock is not
    a reason to reopen a conversation they ended."""
    item_id, variant = _an_out_of_stock_variant(loom)
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    claims = mandate.verify(token)
    loom.post("/reserve",
              json={"item_id": item_id, "variant": variant, "qty": 1,
                    "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"},
              headers={"Authorization": f"Mandate {token}"}).raise_for_status()

    mandate.revoke(claims["jti"])
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 10})

    assert agent_client.get("/agent/offers",
                            params={"shop": "apparel",
                                    "shopper_ref": "shp_zoya"}).json()["offers"] == []
    declined = next(e for e in chainlog.tail("buyer", 40) if e["event"] == "comeback_declined")
    assert "revoked" in declined["why"]


def test_an_expired_session_still_gets_the_offer(agent_client):
    """A session mandate lives an hour; a restock rarely does. An ended session
    is not a withdrawn permission - and an offer is only an offer."""
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=1)
    jti = mandate.verify(token)["jti"]

    resp = agent_client.post("/callback/restock", json={
        "shop_id": "loomcraft", "res_id": "res_x", "product_id": "kurti-indigo-cotton",
        "product_name": "Indigo Cotton Kurti", "variant": "S", "qty": 1,
        "unit_price_paise": 149900, "shopper_ref": "shp_zoya", "mandate_jti": jti,
    })
    assert resp.json()["offered"] is True


def test_an_offer_needs_some_way_to_reach_the_shopper(agent_client):
    """Neither half of identity present means there is nobody to tell."""
    resp = agent_client.post("/callback/restock", json={
        "shop_id": "loomcraft", "res_id": "res_y", "product_id": "kurti-indigo-cotton",
        "product_name": "Indigo Cotton Kurti", "variant": "S", "qty": 1,
        "unit_price_paise": 149900, "shopper_ref": "", "contact_key": "",
        "mandate_jti": "abc123",
    })
    assert resp.json()["offered"] is False


def test_a_contact_key_alone_is_enough_to_reach_them(agent_client):
    """The browser that took the reservation may be gone. The contact is not."""
    resp = agent_client.post("/callback/restock", json={
        "shop_id": "loomcraft", "res_id": "res_z", "product_id": "kurti-indigo-cotton",
        "product_name": "Indigo Cotton Kurti", "variant": "S", "qty": 1,
        "unit_price_paise": 149900, "shopper_ref": "", "contact_key": "phone:9821158848",
        "mandate_jti": "abc123",
    })
    assert resp.json()["offered"] is True

    # and it surfaces for that contact on a browser that has never been seen
    offers = agent_client.get("/agent/offers",
                              params={"shop": "apparel", "shopper_ref": "shp_brand_new_device",
                                      "contact_key": "phone:9821158848"}).json()["offers"]
    assert len(offers) == 1
    assert offers[0]["res_id"] == "res_z"


def test_one_shoppers_offer_is_not_shown_to_another(loom, agent_client):
    item_id, variant = _an_out_of_stock_variant(loom)
    _reserve(loom, item_id, variant, shopper_ref="shp_zoya")
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 8})

    assert agent_client.get("/agent/offers",
                            params={"shop": "apparel",
                                    "shopper_ref": "shp_someone_else"}).json()["offers"] == []


def test_an_offer_can_only_be_acted_on_once(loom, agent_client):
    """A double tap must not become two comeback sales."""
    item_id, variant = _an_out_of_stock_variant(loom)
    _reserve(loom, item_id, variant)
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 8})
    offer_id = agent_client.get(
        "/agent/offers",
        params={"shop": "apparel", "shopper_ref": "shp_zoya"}).json()["offers"][0]["offer_id"]

    assert runtime.take_offer(offer_id) is not None
    assert runtime.take_offer(offer_id) is None
    assert agent_client.get("/agent/offers",
                            params={"shop": "apparel",
                                    "shopper_ref": "shp_zoya"}).json()["offers"] == []


def test_declining_clears_the_offer_and_is_logged(loom, agent_client):
    item_id, variant = _an_out_of_stock_variant(loom)
    _reserve(loom, item_id, variant)
    loom.post("/admin/restock", json={"item_id": item_id, "variant": variant, "qty": 8})
    offer_id = agent_client.get(
        "/agent/offers",
        params={"shop": "apparel", "shopper_ref": "shp_zoya"}).json()["offers"][0]["offer_id"]

    assert agent_client.post(f"/agent/offers/{offer_id}/decline").json()["declined"] is True
    assert agent_client.get("/agent/offers",
                            params={"shop": "apparel",
                                    "shopper_ref": "shp_zoya"}).json()["offers"] == []
    assert any(e["event"] == "comeback_declined" and "declined the restock offer" in e["why"]
               for e in chainlog.tail("buyer", 40))


def test_the_shop_survives_the_agent_service_being_down(loomcraft, monkeypatch):
    """A dead :8003 must not cost the merchant their restock."""
    import shop.app as shop_app

    class Dead:
        @staticmethod
        def post(*a, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(shop_app, "httpx", Dead)
    item_id, variant = _an_out_of_stock_variant(loomcraft)
    token = mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    loomcraft.post("/reserve",
                   json={"item_id": item_id, "variant": variant, "qty": 1,
                         "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"},
                   headers={"Authorization": f"Mandate {token}"}).raise_for_status()

    result = loomcraft.post("/admin/restock",
                            json={"item_id": item_id, "variant": variant, "qty": 6}).json()
    assert result["stock"] == 6                                  # the stock still went in
    assert result["reservations_notified"][0]["accepted"] is False

    queue = loomcraft.get("/merchant/reservations").json()
    row = next(r for r in queue["reservations"] if r["item_id"] == item_id)
    assert row["status"] == "open"      # still owed a callback, not silently lost


def test_freshkart_cannot_take_a_reservation_at_all(freshkart):
    """Capability gating (spec 6.1): the comeback mechanic is Loomcraft's."""
    token = mandate.issue(10_000_000, 5_000_000, ["freshkart"], ttl_seconds=600)
    resp = freshkart.post("/reserve",
                          json={"item_id": "ghee-500ml", "variant": "", "qty": 1,
                                "contact_ref": "x@example.com"},
                          headers={"Authorization": f"Mandate {token}"})
    assert resp.json()["code"] == "CAPABILITY_UNSUPPORTED"


def test_a_reservation_with_no_browser_is_still_reachable_by_contact(loom):
    """A reservation always collects a contact, so losing the browser no longer
    loses the shopper - which is the whole point of the shopper key."""
    item_id, variant = _an_out_of_stock_variant(loom)
    res_id = _reserve(loom, item_id, variant, shopper_ref="")   # no browser ref at all

    result = loom.post("/admin/restock",
                       json={"item_id": item_id, "variant": variant, "qty": 5}).json()
    row = result["reservations_notified"][0]
    assert row["accepted"] is True
    assert row["offered"] is True      # reached through zoya@example.com

    queue = loom.get("/merchant/reservations").json()
    assert next(r for r in queue["reservations"] if r["res_id"] == res_id)["status"] == "notified"


def test_a_reservation_nobody_could_reach_stays_open(loom, monkeypatch):
    """Delivered is not reached. When the agent declines to act, the shopper is
    still owed a contact, so it stays open work on the merchant's queue."""
    from common import mandate as mandate_mod

    item_id, variant = _an_out_of_stock_variant(loom)
    token = mandate_mod.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600)
    jti = mandate_mod.verify(token)["jti"]
    resp = loom.post("/reserve",
                     json={"item_id": item_id, "variant": variant, "qty": 1,
                           "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"},
                     headers={"Authorization": f"Mandate {token}"})
    res_id = resp.json()["res_id"]
    mandate_mod.revoke(jti)            # the shopper withdrew consent

    result = loom.post("/admin/restock",
                       json={"item_id": item_id, "variant": variant, "qty": 5}).json()
    row = result["reservations_notified"][0]
    assert row["accepted"] is True     # the callback was delivered
    assert row["offered"] is False     # but nobody was offered anything

    queue = loom.get("/merchant/reservations").json()
    assert next(r for r in queue["reservations"] if r["res_id"] == res_id)["status"] == "open"
