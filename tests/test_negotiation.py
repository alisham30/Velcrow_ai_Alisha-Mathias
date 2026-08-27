"""Phase 11: agent-to-agent price negotiation, bounded by policy in code.

Two autonomous parties with opposed interests. What these tests guard is not
that a deal happens - it is that NO sequence of asks, from either side, can
produce a price below the merchant's margin floor, above the shelf price, or
different from what the signed token says. The model-free arithmetic is the
whole point: a negotiation you cannot predict from the policy is a negotiation
you cannot audit.

Loomcraft's kurti: list 149,900, cost 90,000. With the 12% floor, in integer
basis points (never floats - a float here invented a phantom rupee):
    floor  = ceil(90000 * 11200 / 10000) = 100,800
    target = 100800 + (149900 - 100800) // 2 = 125,350
"""
from __future__ import annotations

import time
import uuid

import pytest

from common import chainlog, mandate
from shop import negotiation

ITEM, VARIANT = "kurti-indigo-cotton", "M"
LIST, FLOOR, TARGET = 149_900, 100_800, 125_350


def _auth(shop_id: str = "loomcraft") -> dict[str, str]:
    return {"Authorization": f"Mandate {mandate.issue(10_000_000, 5_000_000, [shop_id],
                                                      ttl_seconds=600)}"}


def _offer(client, paise, neg_id="", item=ITEM, variant=VARIANT, qty=1, headers=None):
    body = {"item_id": item, "variant": variant, "qty": qty, "offer_paise": paise}
    if neg_id:
        body["neg_id"] = neg_id
    r = client.post("/negotiate", json=body, headers=headers or _auth())
    assert r.status_code == 200, r.text
    return r.json()


# -- the policy is predictable from the docstring -----------------------------

def test_an_offer_below_the_floor_is_refused_with_the_reason(loomcraft):
    a = _offer(loomcraft, 50_000)
    assert a["decision"] == "refused"
    assert "12.0% margin" in a["why"]
    assert a["floor"]["minimum_unit_paise"] == FLOOR
    assert "offer_token" not in a


def test_a_floor_clearing_offer_draws_a_counter_at_target(loomcraft):
    a = _offer(loomcraft, 105_000)
    assert a["decision"] == "counter"
    assert a["unit_price_paise"] == TARGET
    assert a["offer_token"]["unit_price_paise"] == TARGET


def test_an_offer_at_target_is_accepted_at_the_offer(loomcraft):
    a = _offer(loomcraft, 130_000)
    assert a["decision"] == "accepted"
    assert a["unit_price_paise"] == 130_000


def test_offering_above_list_is_accepted_at_list_never_above(loomcraft):
    a = _offer(loomcraft, 200_000)
    assert a["decision"] == "accepted"
    assert a["unit_price_paise"] == LIST


def test_holding_your_ground_closes_the_deal_at_your_offer(loomcraft):
    """Counter received, buyer repeats the same floor-clearing number: the
    shop closes rather than lose the sale. The deal-closing rule is code."""
    first = _offer(loomcraft, 105_000)
    assert first["decision"] == "counter"
    second = _offer(loomcraft, 105_000, neg_id=first["neg_id"])
    assert second["decision"] == "accepted"
    assert second["unit_price_paise"] == 105_000


def test_no_path_leads_below_the_floor(loomcraft):
    """Whatever the sequence, an issued price never breaches the floor."""
    neg = ""
    for paise in (10_000, 100_799, 100_800, 99_000, 100_800):
        a = _offer(loomcraft, paise, neg_id=neg)
        neg = a["neg_id"]
        if "unit_price_paise" in a:
            assert a["unit_price_paise"] >= FLOOR


def test_negotiation_requires_a_mandate(loomcraft):
    r = loomcraft.post("/negotiate", json={"item_id": ITEM, "variant": VARIANT,
                                           "qty": 1, "offer_paise": 80_000})
    assert r.status_code == 402      # the project's convention for a missing mandate
    assert r.json()["code"] == "MANDATE_INVALID"


# -- the signed token is the only thing a price lives in ----------------------

def test_the_token_redeems_at_exactly_the_negotiated_price(loomcraft):
    a = _offer(loomcraft, 130_000)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil", headers=_auth(),
                   json={"item_id": ITEM, "variant": VARIANT, "qty": 1, "mode": "add"})
    placed = loomcraft.post("/order", headers={**_auth(), "Idempotency-Key": uuid.uuid4().hex},
                            json={"cart_id": cart, "offer_token": a["offer_token"]})
    assert placed.status_code == 201, placed.text
    body = placed.json()
    assert body["charge_amount"] == 130_000
    assert body["coupon"]["codes"] == []          # negotiated is final; no stacking
    assert "negotiated" in body["coupon"]["arithmetic"]


def test_a_tampered_token_is_refused(loomcraft):
    a = _offer(loomcraft, 105_000)
    token = dict(a["offer_token"])
    token["unit_price_paise"] = 1                  # help yourself to a discount
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil", headers=_auth(),
                   json={"item_id": ITEM, "variant": VARIANT, "qty": 1, "mode": "add"})
    r = loomcraft.post("/order", headers={**_auth(), "Idempotency-Key": uuid.uuid4().hex},
                       json={"cart_id": cart, "offer_token": token})
    assert r.status_code == 409
    assert r.json()["code"] == "OFFER_INVALID"
    assert "altered" in r.json()["why"]


def test_an_expired_token_is_refused(loomcraft, monkeypatch):
    a = _offer(loomcraft, 130_000)
    monkeypatch.setattr(time, "time", lambda: time.gmtime and __import__("calendar").timegm(
        __import__("time").gmtime()) + negotiation.OFFER_TTL_SECONDS + 5)
    with pytest.raises(Exception):
        negotiation.verify_offer_token(a["offer_token"], "loomcraft")


def test_a_token_spends_exactly_once(loomcraft):
    a = _offer(loomcraft, 130_000)

    def buy():
        cart = loomcraft.post("/cart").json()["cart_id"]
        loomcraft.post(f"/cart/{cart}/fulfil", headers=_auth(),
                       json={"item_id": ITEM, "variant": VARIANT, "qty": 1, "mode": "add"})
        return loomcraft.post("/order",
                              headers={**_auth(), "Idempotency-Key": uuid.uuid4().hex},
                              json={"cart_id": cart, "offer_token": a["offer_token"]})

    assert buy().status_code == 201
    again = buy()
    assert again.status_code == 409
    assert "exactly once" in again.json()["why"]


def test_the_token_cannot_price_a_different_basket(loomcraft):
    """Negotiate one kurti, then try to push two through at that price."""
    a = _offer(loomcraft, 130_000)
    cart = loomcraft.post("/cart").json()["cart_id"]
    loomcraft.post(f"/cart/{cart}/fulfil", headers=_auth(),
                   json={"item_id": ITEM, "variant": VARIANT, "qty": 2, "mode": "add"})
    r = loomcraft.post("/order", headers={**_auth(), "Idempotency-Key": uuid.uuid4().hex},
                       json={"cart_id": cart, "offer_token": a["offer_token"]})
    assert r.status_code == 409
    assert r.json()["code"] == "OFFER_INVALID"


def test_another_shops_token_is_refused(loomcraft, freshkart):
    a = _offer(loomcraft, 130_000)
    with pytest.raises(Exception) as exc:
        negotiation.verify_offer_token(a["offer_token"], "freshkart")
    assert "different shop" in str(exc.value)


# -- both sides keep their own record -----------------------------------------

def test_every_decision_lands_on_the_shops_chain(loomcraft):
    a = _offer(loomcraft, 50_000)
    b = _offer(loomcraft, 105_000, neg_id=a["neg_id"])
    events = [e for e in chainlog.tail("loomcraft", 20)
              if e.get("data", {}).get("neg_id") == a["neg_id"]]
    kinds = [e["event"] for e in events]
    assert "negotiation_refused" in kinds
    assert "negotiation_counter" in kinds
    floors = [e["data"]["floor_paise"] for e in events]
    assert all(f == FLOOR for f in floors)


# -- the buyer's half: deterministic strategy under a capped mandate ----------

class _RoutedHttpx:
    """Route agent.buyer's outbound httpx at an in-process shop."""

    def __init__(self, client):
        self._client = client

    def Client(self, base_url: str = "", timeout: int = 0):
        outer = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, path, **kw):
                return outer._client.post(path, **kw)

            def get(self, path, **kw):
                return outer._client.get(path, **kw)

        return C()


def _shop(loomcraft):
    return {"shop_id": "loomcraft", "name": "Loomcraft", "category": "apparel",
            "url": "http://testshop"}


def test_the_buyer_negotiates_and_buys_within_its_ceiling(loomcraft, monkeypatch):
    """Ceiling 130,000: opens at 104,000 (above floor), draws the counter at
    125,350 (within ceiling), takes the signed token and pays exactly that.
    Both chains carry the same neg_id and the same agreed price."""
    from agent import buyer

    monkeypatch.setattr(buyer, "httpx", _RoutedHttpx(loomcraft))
    token = mandate.issue(130_000, 130_000, ["loomcraft"], ttl_seconds=600)
    result = buyer.negotiate_and_buy(_shop(loomcraft), ITEM, VARIANT, 1, 130_000, token)

    assert result["outcome"] == "bought"
    assert result["unit_price_paise"] == TARGET
    assert result["charge_amount"] == TARGET
    assert loomcraft.get(f"/order/{result['txn_ref']}").json()["status"] == "paid"

    buyer_events = [e for e in chainlog.tail("buyer", 30)
                    if e.get("data", {}).get("neg_id") == result["neg_id"]]
    shop_events = [e for e in chainlog.tail("loomcraft", 30)
                   if e.get("data", {}).get("neg_id") == result["neg_id"]]
    assert any(e["event"] == "negotiated_purchase" for e in buyer_events)
    assert any(e["event"] == "negotiated_price_honoured" for e in shop_events)
    agreed_shop = next(e["data"]["unit_price_paise"] for e in shop_events
                       if e["event"] == "negotiated_price_honoured")
    assert agreed_shop == TARGET


def test_the_buyer_insists_and_the_shop_closes_at_the_ceiling(loomcraft, monkeypatch):
    """Ceiling 110,000: opening 88,000 is refused naming the floor; re-offer
    at the floor draws the counter (125,350, above ceiling); the buyer insists
    at its ceiling twice and the shop's deal-closing rule takes it."""
    from agent import buyer

    monkeypatch.setattr(buyer, "httpx", _RoutedHttpx(loomcraft))
    token = mandate.issue(110_000, 110_000, ["loomcraft"], ttl_seconds=600)
    result = buyer.negotiate_and_buy(_shop(loomcraft), ITEM, VARIANT, 1, 110_000, token)

    assert result["outcome"] == "bought"
    assert result["unit_price_paise"] == 110_000
    assert FLOOR <= result["unit_price_paise"] < LIST


def test_a_ceiling_below_the_floor_walks_away_with_nothing_charged(loomcraft, monkeypatch):
    from agent import buyer

    monkeypatch.setattr(buyer, "httpx", _RoutedHttpx(loomcraft))
    before = loomcraft.get("/merchant/summary").json().get("revenue_paise", 0)
    token = mandate.issue(60_000, 60_000, ["loomcraft"], ttl_seconds=600)
    result = buyer.negotiate_and_buy(_shop(loomcraft), ITEM, VARIANT, 1, 60_000, token)

    assert result["outcome"] == "walked"
    assert result.get("shop_floor_paise") == FLOOR
    after = loomcraft.get("/merchant/summary").json().get("revenue_paise", 0)
    assert after == before
    walked = [e for e in chainlog.tail("buyer", 20)
              if e["event"] == "negotiation_walked"
              and e["data"].get("neg_id") == result["neg_id"]]
    assert walked and "ceiling" in walked[0]["why"]


def test_the_mandate_caps_back_the_ceiling_in_code(loomcraft, monkeypatch):
    """Even a buggy negotiator cannot overspend: the mandate issued for the
    negotiation is capped at ceiling x qty, so a price above it would die at
    the shop's own OverCap check."""
    from agent import buyer

    monkeypatch.setattr(buyer, "httpx", _RoutedHttpx(loomcraft))
    token = mandate.issue(110_000, 110_000, ["loomcraft"], ttl_seconds=600)
    result = buyer.negotiate_and_buy(_shop(loomcraft), ITEM, VARIANT, 1, 110_000, token)
    assert result["charge_amount"] <= 110_000
