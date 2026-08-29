"""Phase 12: cross-sell from real baskets, or honest silence.

The acceptance is entirely about NOT inventing things: every suggestion names
how many paid baskets it rests on, nothing below the support floor is spoken,
and a shop with thin history says nothing rather than something plausible.
The counting is the shop's code; the model only relays a sentence it was
handed, and a fresh shop hands it none.
"""
from __future__ import annotations

import uuid

from common import mandate

FLOOR = 3   # must match SUGGESTION_SUPPORT_FLOOR in shop/app.py


def _auth(shop_id: str = "freshkart") -> dict[str, str]:
    return {"Authorization": f"Mandate {mandate.issue(10_000_000, 5_000_000, [shop_id],
                                                      ttl_seconds=600)}"}


def _buy_basket(client, items: list[tuple[str, int]], shop_id: str = "freshkart") -> None:
    """A real paid order, through the real endpoints - the only thing that
    may ever feed a suggestion."""
    cart = client.post("/cart").json()["cart_id"]
    for item_id, qty in items:
        client.post(f"/cart/{cart}/fulfil", headers=_auth(shop_id),
                    json={"item_id": item_id, "variant": "", "qty": qty, "mode": "add"})
    placed = client.post("/order", headers={**_auth(shop_id),
                                            "Idempotency-Key": uuid.uuid4().hex},
                         json={"cart_id": cart})
    assert placed.status_code == 201, placed.text
    client.post("/confirm-payment",
                json={"txn_ref": placed.json()["txn_ref"],
                      "razorpay_order_id": "order_xsell_test", "payment_ref": "pay_x"})


def _cart_with(client, item_id: str, shop_id: str = "freshkart") -> str:
    cart = client.post("/cart").json()["cart_id"]
    client.post(f"/cart/{cart}/fulfil", headers=_auth(shop_id),
                json={"item_id": item_id, "variant": "", "qty": 1, "mode": "add"})
    return cart


def test_a_fresh_shop_suggests_nothing_rather_than_inventing(freshkart):
    cart = _cart_with(freshkart, "lemons-1kg")
    r = freshkart.get(f"/cart/{cart}/suggestions").json()
    assert r["suggestions"] == []
    assert r["support_floor"] == FLOOR
    assert r["paid_baskets_considered"] == 0
    assert "none rather than" in r["note"] or "thin history" in r["note"]


def test_below_the_floor_stays_silent_at_the_floor_speaks(freshkart):
    """Two shared baskets: silence. A third: the pair clears the floor and is
    reported WITH its count."""
    for _ in range(FLOOR - 1):
        _buy_basket(freshkart, [("lemons-1kg", 1), ("honey-500g", 1)])
    cart = _cart_with(freshkart, "lemons-1kg")
    assert freshkart.get(f"/cart/{cart}/suggestions").json()["suggestions"] == []

    _buy_basket(freshkart, [("lemons-1kg", 1), ("honey-500g", 1)])
    got = freshkart.get(f"/cart/{cart}/suggestions").json()["suggestions"]
    assert len(got) == 1
    assert got[0]["item_id"] == "honey-500g"
    assert got[0]["baskets_together"] == FLOOR
    assert f"in {FLOOR} past baskets" in got[0]["based_on"]


def test_what_is_already_in_the_cart_is_not_suggested_back(freshkart):
    for _ in range(FLOOR):
        _buy_basket(freshkart, [("lemons-1kg", 1), ("honey-500g", 1)])
    cart = freshkart.post("/cart").json()["cart_id"]
    for item in ("lemons-1kg", "honey-500g"):
        freshkart.post(f"/cart/{cart}/fulfil", headers=_auth(),
                       json={"item_id": item, "variant": "", "qty": 1, "mode": "add"})
    assert freshkart.get(f"/cart/{cart}/suggestions").json()["suggestions"] == []


def test_out_of_stock_is_never_recommended(freshkart):
    """Ghee is seeded at zero stock. However strong the pair, a suggestion
    the shopper cannot buy is an advertisement for disappointment."""
    freshkart.post("/admin/restock", json={"item_id": "ghee-500ml", "variant": "", "qty": 10})
    for _ in range(FLOOR):
        _buy_basket(freshkart, [("lemons-1kg", 1), ("ghee-500ml", 1)])
    # Sell the shelf back down to zero.
    _buy_basket(freshkart, [("ghee-500ml", 7)])
    cart = _cart_with(freshkart, "lemons-1kg")
    got = freshkart.get(f"/cart/{cart}/suggestions").json()["suggestions"]
    assert all(sug["item_id"] != "ghee-500ml" for sug in got)


def test_unpaid_orders_feed_nothing(freshkart):
    """A quote nobody paid for is not a basket. Only status='paid' counts."""
    for _ in range(FLOOR + 1):
        cart = freshkart.post("/cart").json()["cart_id"]
        for item in ("lemons-1kg", "atta-5kg"):
            freshkart.post(f"/cart/{cart}/fulfil", headers=_auth(),
                           json={"item_id": item, "variant": "", "qty": 1, "mode": "add"})
        freshkart.post("/order", headers={**_auth(), "Idempotency-Key": uuid.uuid4().hex},
                       json={"cart_id": cart})     # placed, never confirmed
    probe = _cart_with(freshkart, "lemons-1kg")
    assert freshkart.get(f"/cart/{probe}/suggestions").json()["suggestions"] == []


def test_the_agents_add_result_carries_the_shops_sentence(freshkart, monkeypatch):
    """The model never counts baskets: the tool result hands it one suggestion
    with the count already written into a sentence, or nothing at all."""
    from agent import tools

    for _ in range(FLOOR):
        _buy_basket(freshkart, [("lemons-1kg", 1), ("honey-500g", 1)])

    class Routed:
        def __init__(self, base_url: str = "", timeout: int = 0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, **kw):
            return freshkart.get(path, **kw)

        def post(self, path, **kw):
            return freshkart.post(path, **kw)

        def patch(self, path, **kw):
            return freshkart.patch(path, **kw)

    monkeypatch.setattr(tools, "_client", lambda url: Routed())
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart",
           "cart_id": freshkart.post("/cart").json()["cart_id"],
           "mandate_token": mandate.issue(10_000_000, 5_000_000, ["freshkart"],
                                          ttl_seconds=600)}
    result = tools.add_to_cart(ctx, item_id="lemons-1kg", qty=1)
    also = result.get("also_bought")
    assert also and also["item_id"] == "honey-500g"
    assert also["baskets_together"] >= FLOOR
    assert f"in {also['baskets_together']} past baskets" in also["tell_the_shopper"]
    assert "mention it once" in also["tell_the_shopper"]


def test_an_empty_history_reaches_the_agent_as_absence_not_zero(freshkart, monkeypatch):
    from agent import tools

    class Routed:
        def __init__(self, base_url: str = "", timeout: int = 0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, **kw):
            return freshkart.get(path, **kw)

        def post(self, path, **kw):
            return freshkart.post(path, **kw)

    monkeypatch.setattr(tools, "_client", lambda url: Routed())
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart",
           "cart_id": freshkart.post("/cart").json()["cart_id"],
           "mandate_token": mandate.issue(10_000_000, 5_000_000, ["freshkart"],
                                          ttl_seconds=600)}
    result = tools.add_to_cart(ctx, item_id="lemons-1kg", qty=1)
    assert "also_bought" not in result
