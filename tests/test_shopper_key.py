"""The shopper key (spec 7.3): purchase history outlives the process.

Reorder and the comeback sale are two of the four revenue mechanics, and both
are worthless if identity lives in memory. These tests destroy every in-memory
structure between writing an order and reading it back - a new app object, a
new ShopDB, a cleared conversation store, a cleared offer store - so anything
that survives did so because it was on disk and resolved through the key.

What is NOT expected to survive is stated explicitly at the bottom: the
conversation thread is in-process and dies with the service. That is fine. It
must simply never be the thing that loses an order.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import runtime, tools
from common import contact, mandate


def _fresh_shop(monkeypatch, kind: str = "grocery") -> TestClient:
    """A brand-new app object and a brand-new ShopDB over the same files."""
    from shop.app import create_app

    monkeypatch.setenv("SHOP", kind)
    return TestClient(create_app(), raise_server_exceptions=True)


def _buy(client: TestClient, items: list[tuple[str, int]], *,
         shopper_ref: str = "", contact_text: str = "") -> str:
    """Drive a real purchase end to end and return its txn_ref."""
    cart_id = client.post("/cart").json()["cart_id"]
    for item_id, qty in items:
        client.patch(f"/cart/{cart_id}",
                     json={"op": "add", "item_id": item_id, "variant": "", "qty": qty}
                     ).raise_for_status()
    token = mandate.issue(10_000_000, 5_000_000, ["freshkart"], ttl_seconds=600)
    order = client.post("/order",
                        json={"cart_id": cart_id, "shopper_ref": shopper_ref,
                              "contact": contact_text},
                        headers={"Authorization": f"Mandate {token}"}).json()
    client.post("/confirm-payment",
                json={"txn_ref": order["txn_ref"], "razorpay_order_id": "order_test",
                      "payment_ref": "pay_test"}).raise_for_status()
    return order["txn_ref"]


# -- contact normalisation --------------------------------------------------

@pytest.mark.parametrize("typed", [
    "9821158848", "+91 98211 58848", "098211 58848", "+919821158848", "98211-58848",
])
def test_one_phone_number_typed_five_ways_is_one_shopper(typed):
    assert contact.normalise(typed) == "phone:9821158848"


@pytest.mark.parametrize("typed", ["zoya@example.com", "  Zoya@Example.COM  "])
def test_one_email_typed_two_ways_is_one_shopper(typed):
    assert contact.normalise(typed) == "email:zoya@example.com"


@pytest.mark.parametrize("bad", ["", "   ", "12345", "not-an-email@", "@nope.com", "a@b"])
def test_an_unusable_contact_is_refused_not_silently_accepted(bad):
    with pytest.raises(contact.InvalidContact):
        contact.normalise(bad)


def test_two_different_shoppers_do_not_collide():
    assert contact.normalise("9821158848") != contact.normalise("9821158849")
    assert contact.normalise("a@example.com") != contact.normalise("b@example.com")


# -- the load-bearing test: history outlives the process --------------------

def test_history_survives_a_full_teardown_of_in_memory_state(env, monkeypatch):
    """Write an order, destroy everything held in memory, read it back."""
    shop_a = _fresh_shop(monkeypatch)
    txn = _buy(shop_a, [("lemons-1kg", 2), ("honey-500g", 1)],
               shopper_ref="shp_laptop", contact_text="9821158848")

    # tear down every in-memory structure the system has
    del shop_a
    runtime.CONVERSATIONS.clear()
    runtime.OFFERS.clear()
    runtime.RUNS.clear()

    shop_b = _fresh_shop(monkeypatch)          # new app, new ShopDB, same disk
    key = contact.normalise("9821158848")
    past = shop_b.get("/orders/last", params={"contact_key": key}).json()

    assert past["txn_ref"] == txn
    assert {l["item_id"] for l in past["lines"]} == {"lemons-1kg", "honey-500g"}
    assert past["now_subtotal_paise"] == 2 * 4400 + 21900


def test_the_agent_answers_a_history_question_after_a_teardown(env, monkeypatch):
    """The whole path: contact key -> shop -> re-quote with deltas."""
    shop_a = _fresh_shop(monkeypatch)
    _buy(shop_a, [("lemons-1kg", 2)], shopper_ref="shp_laptop", contact_text="9821158848")
    del shop_a
    runtime.CONVERSATIONS.clear()

    shop_b = _fresh_shop(monkeypatch)

    class Client:
        def __init__(self, url: str, timeout: int = 0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path: str, params: dict[str, Any] | None = None):
            return shop_b.get(path, params=params)

        def post(self, path: str, json=None, headers=None):
            return shop_b.post(path, json=json, headers=headers)

    monkeypatch.setattr(tools, "_client", lambda url: Client(url))

    # a brand-new browser: no shopper_ref at all, only the contact
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart",
           "cart_id": shop_b.post("/cart").json()["cart_id"],
           "shopper_ref": "", "contact_key": contact.normalise("+91 98211 58848")}
    result = tools.reorder_last(ctx)

    assert [l["item_id"] for l in result["lines"]] == ["lemons-1kg"]
    assert result["lines"][0]["qty"] == 2
    assert result["now_subtotal_display"] == "₹88.00"
    assert result["total_delta_display"] == "the same as last time"


def test_one_contact_resolves_across_every_device_it_was_used_on(env, monkeypatch):
    """The explicit ask: history spans the several refs linked to one contact."""
    shop_a = _fresh_shop(monkeypatch)
    _buy(shop_a, [("lemons-1kg", 1)], shopper_ref="shp_laptop", contact_text="9821158848")
    _buy(shop_a, [("atta-5kg", 1)], shopper_ref="shp_phone", contact_text="+91 98211 58848")
    latest = _buy(shop_a, [("honey-500g", 2)], shopper_ref="shp_tablet",
                  contact_text="098211 58848")
    del shop_a
    runtime.CONVERSATIONS.clear()

    shop_b = _fresh_shop(monkeypatch)
    key = contact.normalise("9821158848")

    # three browsers, one person, and the newest basket wins
    past = shop_b.get("/orders/last", params={"contact_key": key}).json()
    assert past["txn_ref"] == latest
    assert [l["item_id"] for l in past["lines"]] == ["honey-500g"]

    # and a fourth, never-seen browser finds it with the contact alone
    on_a_new_device = shop_b.get("/orders/last",
                                 params={"shopper_ref": "shp_never_seen",
                                         "contact_key": key}).json()
    assert on_a_new_device["txn_ref"] == latest


def test_giving_a_contact_claims_orders_bought_before_it_was_given(env, monkeypatch):
    """A shopper who bought anonymously still finds that basket the moment
    they identify - otherwise the key only works from today onwards."""
    shop_a = _fresh_shop(monkeypatch)
    anon = _buy(shop_a, [("lemons-1kg", 3)], shopper_ref="shp_laptop")   # no contact
    del shop_a

    shop_b = _fresh_shop(monkeypatch)
    assert shop_b.get("/orders/last",
                      params={"contact_key": contact.normalise("9821158848")}
                      ).status_code == 404          # nothing under the contact yet

    result = shop_b.post("/shopper/identify",
                         json={"contact": "9821158848",
                               "shopper_ref": "shp_laptop"}).json()
    assert result["orders_claimed"] == 1
    assert result["has_history"] is True

    del shop_b
    shop_c = _fresh_shop(monkeypatch)              # and it survives another teardown
    past = shop_c.get("/orders/last",
                      params={"contact_key": contact.normalise("9821158848")}).json()
    assert past["txn_ref"] == anon


def test_a_purchase_made_through_the_storefront_is_not_anonymous(env, monkeypatch):
    """The original defect: /order accepted no shopper key, so every storefront
    purchase was written anonymous and reorder could never see it."""
    client = _fresh_shop(monkeypatch)
    txn = _buy(client, [("lemons-1kg", 1)], shopper_ref="shp_laptop",
               contact_text="zoya@example.com")

    order = client.get(f"/order/{txn}").json()
    assert order["status"] == "paid"
    stored = client.get("/orders/last",
                        params={"contact_key": contact.normalise("zoya@example.com")}).json()
    assert stored["txn_ref"] == txn


def test_one_shoppers_history_is_not_returned_for_another_contact(env, monkeypatch):
    client = _fresh_shop(monkeypatch)
    _buy(client, [("lemons-1kg", 1)], shopper_ref="shp_a", contact_text="9821158848")
    assert client.get("/orders/last",
                      params={"contact_key": contact.normalise("9000000000")}
                      ).status_code == 404


def test_an_unusable_contact_never_blocks_a_sale(env, monkeypatch):
    """A malformed contact costs the shopper their history, never their order."""
    client = _fresh_shop(monkeypatch)
    txn = _buy(client, [("lemons-1kg", 1)], shopper_ref="shp_laptop", contact_text="???")
    assert client.get(f"/order/{txn}").json()["status"] == "paid"


# -- what is deliberately NOT persistent ------------------------------------

def test_the_conversation_thread_does_not_survive_a_restart_and_that_is_fine(env, monkeypatch):
    """Stated plainly rather than pretended: CONVERSATIONS is in-process and
    dies with :8003, so a chat cannot resume after a restart. What must NOT
    happen is that taking the thread away also takes the order history."""
    client = _fresh_shop(monkeypatch)
    _buy(client, [("lemons-1kg", 2)], shopper_ref="shp_laptop", contact_text="9821158848")

    runtime.CONVERSATIONS[("freshkart", "cart_x")] = [{"role": "user", "content": "hello"}]
    runtime.CONVERSATIONS.clear()                      # the restart
    assert runtime.CONVERSATIONS == {}                 # the thread is gone

    past = client.get("/orders/last",
                      params={"contact_key": contact.normalise("9821158848")}).json()
    assert past["lines"][0]["item_id"] == "lemons-1kg"  # the history is not
