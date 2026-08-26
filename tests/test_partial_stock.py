"""Asking for more than the shelf holds (spec 7.2).

"Add 16 wool scarves" when 12 exist used to be a flat refusal at the cart and
a question from the agent, so the shopper got nothing and the merchant lost
all 16 rather than selling 12 and holding 4. The split lives in one shop
endpoint that both the storefront and the agent call, so it cannot drift
between the two surfaces.
"""
from __future__ import annotations

import pytest

from agent import tools
from common import mandate


def _auth(shop_id: str = "loomcraft") -> dict[str, str]:
    token = mandate.issue(10_000_000, 5_000_000, [shop_id], ttl_seconds=600)
    return {"Authorization": f"Mandate {token}"}


def _short_variant(client) -> tuple[str, str, int]:
    """A product with some stock, so a big enough order overshoots it."""
    for p in client.get("/catalog").json():
        for v in p.get("variants") or []:
            if 0 < v["stock"] <= 20:
                return p["id"], v["label"], v["stock"]
    raise AssertionError("need a variant with limited stock")


def test_the_shelf_is_taken_and_the_rest_is_held(loomcraft):
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    want = stock + 4

    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": want,
                                  "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"},
                            headers=_auth()).json()

    assert result["requested"] == want
    assert result["added"] == stock
    assert result["shortfall"] == 4
    assert result["reserved"] == 4
    assert result["reservation"]["qty"] == 4
    assert result["cart"]["items"][0]["qty"] == stock


def test_the_held_units_are_not_in_the_basket(loomcraft):
    """Reserved is not bought. The cart must show only what will be paid for."""
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil",
                   json={"item_id": item_id, "variant": variant, "qty": stock + 5,
                         "contact_ref": "zoya@example.com"}, headers=_auth())
    view = loomcraft.get(f"/cart/{cart}").json()
    assert sum(l["qty"] for l in view["items"]) == stock


def test_the_shortfall_is_valued_in_the_demand_ledger(loomcraft):
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": stock + 3,
                                  "contact_ref": "zoya@example.com"}, headers=_auth()).json()

    ledger = loomcraft.get("/merchant/demand-ledger").json()
    row = next(r for r in ledger["rows"] if r["item_id"] == item_id and r["variant"] == variant)
    assert row["lost_units"] == 3
    assert row["lost_value_paise"] == 3 * result["unit_price_paise"]


def test_a_request_the_shelf_covers_reserves_nothing(loomcraft):
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": 1},
                            headers=_auth()).json()
    assert result["added"] == 1 and result["shortfall"] == 0 and result["reservation"] is None


def test_a_shop_without_reservations_still_records_what_it_could_not_sell(freshkart):
    """FreshKart takes no reservations, so nobody can be told it is coming
    back - but the merchant must still see the demand they missed."""
    cart = freshkart.post("/cart").json()["cart_id"]
    result = freshkart.post(f"/cart/{cart}/fulfil",
                            json={"item_id": "lemons-1kg", "variant": "", "qty": 34},
                            headers=_auth("freshkart")).json()

    assert result["added"] == 30 and result["shortfall"] == 4
    assert result["reserved"] == 0 and result["can_reserve"] is False
    ledger = freshkart.get("/merchant/demand-ledger").json()
    row = next(r for r in ledger["rows"] if r["item_id"] == "lemons-1kg")
    assert row["lost_units"] == 4


def test_reserving_is_allowed_for_a_shortfall_not_only_a_bare_shelf(loomcraft):
    """The old guard refused any reservation while stock > 0, which is why a
    partial shortfall could not be held at all."""
    item_id, variant, stock = _short_variant(loomcraft)
    ok = loomcraft.post("/reserve",
                        json={"item_id": item_id, "variant": variant, "qty": stock + 2,
                              "contact_ref": "zoya@example.com"}, headers=_auth())
    assert ok.status_code == 201

    covered = loomcraft.post("/reserve",
                             json={"item_id": item_id, "variant": variant, "qty": 1,
                                   "contact_ref": "zoya@example.com"}, headers=_auth())
    assert covered.json()["code"] == "NOT_OUT_OF_STOCK"


# -- the agent surface ------------------------------------------------------

@pytest.fixture
def loom_ctx(loomcraft, monkeypatch):
    class FakeClient:
        def __init__(self, base_url, timeout=0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, params=None):
            return loomcraft.get(path, params=params)

        def post(self, path, json=None, headers=None):
            return loomcraft.post(path, json=json, headers=headers)

        def patch(self, path, json=None):
            return loomcraft.patch(path, json=json)

    monkeypatch.setattr(tools, "_client", lambda url: FakeClient(url))
    return {"shop_url": "http://testshop", "shop_id": "loomcraft",
            "cart_id": loomcraft.post("/cart").json()["cart_id"],
            "mandate_token": mandate.issue(10_000_000, 5_000_000, ["loomcraft"], ttl_seconds=600),
            "contact_ref": "zoya@example.com", "shopper_ref": "shp_zoya"}


def test_the_agent_splits_without_being_asked(loomcraft, loom_ctx):
    """It must not come back with "shall I add 12 instead?" - by the time the
    model sees the result, the 12 are in and the 4 are held."""
    item_id, variant, stock = _short_variant(loomcraft)
    result = tools.add_to_cart(loom_ctx, item_id=item_id, qty=stock + 4, variant=variant)

    assert result["shortfall"]["added"] == stock
    assert result["shortfall"]["short_by"] == 4
    assert result["shortfall"]["reserved"] == 4
    assert "reserved the other 4" in result["tell_the_shopper"]
    assert result["shortfall"]["value_display"].startswith("₹")


def test_the_trace_strip_shows_the_split(loomcraft, loom_ctx):
    item_id, variant, stock = _short_variant(loomcraft)
    result = tools.add_to_cart(loom_ctx, item_id=item_id, qty=stock + 4, variant=variant)
    line = tools.summarise("add_to_cart", result)
    assert f"{stock}/{stock + 4} in stock" in line
    assert "4 reserved" in line


def test_a_bare_shelf_is_held_in_full_where_reservations_exist(loomcraft, loom_ctx):
    out_of_stock = None
    for p in loomcraft.get("/catalog").json():
        for v in p.get("variants") or []:
            if v["stock"] == 0:
                out_of_stock = (p["id"], v["label"])
    assert out_of_stock, "need a fully out-of-stock variant"

    result = tools.add_to_cart(loom_ctx, item_id=out_of_stock[0], qty=2,
                               variant=out_of_stock[1])
    assert result["shortfall"]["added"] == 0
    assert result["shortfall"]["reserved"] == 2


def test_nothing_added_and_nothing_held_is_still_a_refusal(freshkart, monkeypatch):
    """Politeness from the shop is not success: if the shopper got nothing,
    the agent must see a refusal rather than a cheerful empty result."""
    class FakeClient:
        def __init__(self, base_url, timeout=0):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path, params=None):
            return freshkart.get(path, params=params)

        def post(self, path, json=None, headers=None):
            return freshkart.post(path, json=json, headers=headers)

        def patch(self, path, json=None):
            return freshkart.patch(path, json=json)

    monkeypatch.setattr(tools, "_client", lambda url: FakeClient(url))
    ctx = {"shop_url": "http://testshop", "shop_id": "freshkart",
           "cart_id": freshkart.post("/cart").json()["cart_id"],
           "mandate_token": mandate.issue(10_000_000, 5_000_000, ["freshkart"], ttl_seconds=600)}

    with pytest.raises(tools.ToolError) as exc:
        tools.add_to_cart(ctx, item_id="ghee-500ml", qty=1, variant="")
    assert exc.value.code == "OUT_OF_STOCK"
    assert "cannot hold any" in exc.value.message


def test_the_model_is_never_shown_a_stock_number_to_cap_to(loomcraft, loom_ctx):
    """Given an exact count the model quietly lowered the request to match it,
    so the shop never learned the rest was wanted and the shortfall was never
    reserved. It gets a coarse label instead; the shop owns the arithmetic."""
    found = tools.search_catalog(loom_ctx, query="kurti")
    match = found["matches"][0]
    assert "stock" not in match
    for v in match["variants"]:
        assert "stock" not in v
        assert v["availability"] in ("in stock", "only a few left", "out of stock")


def test_availability_labels_map_to_the_shelf():
    assert tools._availability(0) == "out of stock"
    assert tools._availability(3) == "only a few left"
    assert tools._availability(40) == "in stock"


def test_raising_a_line_beyond_stock_splits_instead_of_failing(loomcraft, loom_ctx):
    """"make that 10" when 5 exist used to be a flat refusal, which left the
    shopper stuck with whatever the first add happened to put in."""
    item_id, variant, stock = _short_variant(loomcraft)
    tools.add_to_cart(loom_ctx, item_id=item_id, qty=1, variant=variant)
    cart = tools.view_cart(loom_ctx)
    line_id = cart["lines"][0]["line_id"]

    result = tools.update_qty(loom_ctx, line_id=line_id, qty=stock + 6)
    assert result["shortfall"]["reserved"] > 0
    assert result["item_count"] == stock


def test_a_full_basket_is_explained_not_described_as_a_partial_add(loomcraft, loom_ctx):
    """The basket already holds everything the shelf had, so nothing can be
    added. Saying "only 0 were in stock, so I added those" is nonsense, and
    reads to a shopper as a broken app."""
    item_id, variant, stock = _short_variant(loomcraft)
    tools.add_to_cart(loom_ctx, item_id=item_id, qty=stock, variant=variant)

    # the agent asks for five MORE; the shelf is empty, so all five are held
    again = tools.add_to_cart(loom_ctx, item_id=item_id, qty=5, variant=variant)
    assert again["shortfall"]["added"] == 0
    assert again["shortfall"]["reserved"] == 5
    told = again["tell_the_shopper"]
    assert f"already holds all {stock}" in told
    assert "I added those" not in told


def test_an_empty_shelf_is_worded_as_an_empty_shelf(loomcraft, loom_ctx):
    out = None
    for p in loomcraft.get("/catalog").json():
        for v in p.get("variants") or []:
            if v["stock"] == 0:
                out = (p["id"], v["label"])
    assert out
    result = tools.add_to_cart(loom_ctx, item_id=out[0], qty=3, variant=out[1])
    told = result["tell_the_shopper"]
    assert "there were none left of the 3" in told
    assert "I added those" not in told


def test_the_shop_reports_what_the_basket_already_held(loomcraft):
    """The number the copy needs to explain a zero add."""
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil",
                   json={"item_id": item_id, "variant": variant, "qty": stock},
                   headers=_auth())
    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": 5,
                                  "contact_ref": "zoya@example.com"}, headers=_auth()).json()
    assert result["already_in_cart"] == stock
    assert result["outstanding"] == 5
    assert result["added"] == 0 and result["shortfall"] == 5
    assert result["in_stock_now"] == 0


def test_a_quantity_already_in_the_basket_asks_for_nothing_more(loomcraft):
    """The case from the screenshot, stated as a rule: a line already in the
    basket reads the quantity as the TOTAL wanted. Asking for 9 with 6 there
    leaves 3 outstanding, not 9 - otherwise the shopper ends up committed to
    15 of something they asked 9 for."""
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil",
                   json={"item_id": item_id, "variant": variant, "qty": stock},
                   headers=_auth())

    want = stock + 3
    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": want,
                                  "mode": "target",
                                  "contact_ref": "zoya@example.com"}, headers=_auth()).json()
    assert result["outstanding"] == 3
    assert result["reserved"] == 3
    assert result["already_in_cart"] + result["added"] + result["reserved"] == want


def test_asking_for_fewer_than_the_basket_holds_does_nothing(loomcraft):
    item_id, variant, stock = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil",
                   json={"item_id": item_id, "variant": variant, "qty": stock},
                   headers=_auth())
    result = loomcraft.post(f"/cart/{cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": 1,
                                  "mode": "target"},
                            headers=_auth()).json()
    assert result["outstanding"] == 0
    assert result["added"] == 0 and result["reserved"] == 0


def test_the_two_modes_are_explicit_and_differ(loomcraft):
    """The bug this exists to prevent: "9" meaning nine MORE on one surface and
    nine IN TOTAL on another, with the shop guessing which."""
    item_id, variant, stock = _short_variant(loomcraft)

    def fresh_cart_with(n):
        cart = loomcraft.post("/cart").json()["cart_id"]
        loomcraft.post(f"/cart/{cart}/fulfil",
                       json={"item_id": item_id, "variant": variant, "qty": n},
                       headers=_auth())
        return cart

    want = stock + 3
    add_cart = fresh_cart_with(stock)
    as_add = loomcraft.post(f"/cart/{add_cart}/fulfil",
                            json={"item_id": item_id, "variant": variant, "qty": want,
                                  "mode": "add", "contact_ref": "z@example.com"},
                            headers=_auth()).json()

    target_cart = fresh_cart_with(stock)
    as_target = loomcraft.post(f"/cart/{target_cart}/fulfil",
                               json={"item_id": item_id, "variant": variant, "qty": want,
                                     "mode": "target", "contact_ref": "z@example.com"},
                               headers=_auth()).json()

    assert as_add["outstanding"] == want          # that many more, on top
    assert as_target["outstanding"] == 3          # that many in total
    assert as_target["already_in_cart"] + as_target["reserved"] == want


def test_an_unknown_mode_is_refused(loomcraft):
    item_id, variant, _ = _short_variant(loomcraft)
    cart = loomcraft.post("/cart").json()["cart_id"]
    resp = loomcraft.post(f"/cart/{cart}/fulfil",
                          json={"item_id": item_id, "variant": variant, "qty": 1,
                                "mode": "whatever"}, headers=_auth())
    assert resp.json()["code"] == "BAD_REQUEST"
