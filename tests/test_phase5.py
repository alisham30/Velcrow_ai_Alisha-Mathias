"""Phase 5 (spec 12): widget commerce - coupon claim + near-miss, reorder my
usual, and conversational checkout behind the cart-bound approval of spec 5.1.

The load-bearing claim under test is the negative one: the agent gained three
new tools and still cannot move money. Every path to a charge goes through a
human tap, and the wallet's five checks run after that tap regardless.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent import llm, runtime, tools
from common import approval, chainlog, mandate, wallet


@pytest.fixture
def shop_ctx(freshkart, monkeypatch):
    """Point the agent's HTTP calls at the in-process shop."""

    class FakeClient:
        def __init__(self, base_url: str, timeout: int = 0) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path: str, params: dict[str, Any] | None = None):
            return freshkart.get(path, params=params)

        def post(self, path: str, json=None, headers=None):
            return freshkart.post(path, json=json, headers=headers)

        def patch(self, path: str, json=None):
            return freshkart.patch(path, json=json)

    monkeypatch.setattr(tools, "_client", lambda url: FakeClient(url))
    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)
    cart_id = freshkart.post("/cart").json()["cart_id"]
    token = mandate.issue(10_000_000, 5_000_000, ["freshkart"], ttl_seconds=600)
    return {
        "shop": {"shop_id": "freshkart", "name": "FreshKart", "category": "grocery",
                 "url": "http://testshop"},
        "cart_id": cart_id,
        "client": freshkart,
        "token": token,
        "ctx": {"shop_url": "http://testshop", "shop_id": "freshkart", "cart_id": cart_id,
                "mandate_token": token, "shopper_ref": "shp_test", "turn_id": "t1"},
    }


def _add(shop_ctx, item_id: str, qty: int, variant: str = "") -> None:
    shop_ctx["client"].patch(
        f"/cart/{shop_ctx['cart_id']}",
        json={"op": "add", "item_id": item_id, "variant": variant, "qty": qty},
    ).raise_for_status()


# -- coupons (spec 7.1) -----------------------------------------------------

def test_best_coupons_are_claimed_with_the_arithmetic_shown(shop_ctx):
    _add(shop_ctx, "basmati-5kg", 1)   # Rs 429.00, staples
    result = tools.apply_best_coupons(shop_ctx["ctx"])

    # over Rs 399 so FRESH50 applies; STAPLES10 stacks on a staples line
    assert set(result["claimed"]) == {"FRESH50", "STAPLES10"}
    assert result["subtotal_display"] == "₹429.00"
    assert result["discount_display"] == "₹92.90"   # 50 + 42.90 (10% of 429)
    assert result["net_total_display"] == "₹336.10"
    assert "₹429.00" in result["arithmetic"] and "FRESH50" in result["arithmetic"]


def test_a_cart_below_every_minimum_claims_nothing_and_says_so(shop_ctx):
    _add(shop_ctx, "lemons-1kg", 1)  # Rs 44.00, produce: no coupon reaches it
    result = tools.apply_best_coupons(shop_ctx["ctx"])
    assert result["claimed"] == []
    assert result["net_total_display"] == result["subtotal_display"] == "₹44.00"


def test_near_miss_carries_the_exact_top_up_math(shop_ctx):
    """Spec 7.1: the missed saving is half the point."""
    _add(shop_ctx, "honey-500g", 1)     # 219.00, packaged
    _add(shop_ctx, "lemons-1kg", 2)     #  88.00, produce
    _add(shop_ctx, "tomatoes-1kg", 2)   #  64.00, produce  -> 371.00
    result = tools.apply_best_coupons(shop_ctx["ctx"])

    assert result["claimed"] == []            # nothing reaches this cart yet
    assert result["subtotal_display"] == "₹371.00"
    near = result["near_miss"]
    assert near["code"] == "FRESH50"
    assert near["add_display"] == "₹28.00"        # 371.00 -> the 399.00 minimum
    assert near["unlocks_display"] == "₹50.00"
    assert near["projected_net_display"] == "₹349.00"
    assert near["saves_display"] == "₹22.00"
    assert "₹28.00" in near["math"] and "FRESH50" in near["math"]
    # and it is a genuine improvement, not a nudge for its own sake
    assert near["projected_net_paise"] < result["net_total_paise"]


def test_no_near_miss_when_topping_up_would_cost_more_than_it_saves(shop_ctx):
    """A coupon reachable in Rs 124 that only pays back Rs 50 is not an offer,
    and the agent must not raise it."""
    _add(shop_ctx, "atta-5kg", 1)   # 275.00 staples; STAPLES10 already beats it
    result = tools.apply_best_coupons(shop_ctx["ctx"])
    assert result["claimed"] == ["STAPLES10"]
    assert "near_miss" not in result


def test_coupons_that_did_not_apply_are_reported_not_hidden(shop_ctx):
    _add(shop_ctx, "basmati-5kg", 1)
    result = tools.apply_best_coupons(shop_ctx["ctx"])
    codes = {a["code"] for a in result["applicable"]}
    assert codes == set(result["claimed"]) | {a["code"] for a in result["not_claimed"]}


# -- reorder (spec 6.3, 7.3) ------------------------------------------------

def _complete_an_order(shop_ctx, shopper_ref: str = "shp_test") -> str:
    """Drive a real purchase so there is a past basket to reorder."""
    client, token = shop_ctx["client"], shop_ctx["token"]
    order = client.post("/order",
                        json={"cart_id": shop_ctx["cart_id"], "shopper_ref": shopper_ref},
                        headers={"Authorization": f"Mandate {token}"}).json()
    client.post("/confirm-payment",
                json={"txn_ref": order["txn_ref"], "razorpay_order_id": "order_test",
                      "payment_ref": "pay_test"}).raise_for_status()
    return order["txn_ref"]


def test_reorder_requotes_the_last_basket_at_todays_prices(shop_ctx):
    _add(shop_ctx, "lemons-1kg", 2)
    _add(shop_ctx, "atta-5kg", 1)
    _complete_an_order(shop_ctx)

    result = tools.reorder_last(shop_ctx["ctx"])
    by_id = {l["item_id"]: l for l in result["lines"]}
    assert set(by_id) == {"lemons-1kg", "atta-5kg"}
    assert by_id["lemons-1kg"]["qty"] == 2
    assert by_id["lemons-1kg"]["now_price_display"] == "₹44.00"
    assert by_id["lemons-1kg"]["line_total_display"] == "₹88.00"
    assert result["now_subtotal_display"] == "₹363.00"   # 88 + 275


def test_reorder_names_a_price_that_moved(shop_ctx, monkeypatch):
    """Spec 6.3: "re-quotes at today's prices, shows price deltas". A shopper
    saying "the usual" must be told what got dearer before they agree."""
    _add(shop_ctx, "lemons-1kg", 2)
    _complete_an_order(shop_ctx)

    # the shop reprices overnight: Rs 44.00 -> Rs 54.00 a kilo
    from shop.db import ShopDB

    original = ShopDB.product

    def repriced(self, item_id):
        p = original(self, item_id)
        if p is not None and item_id == "lemons-1kg":
            return {**p, "price_paise": 5400}
        return p

    monkeypatch.setattr(ShopDB, "product", repriced)

    result = tools.reorder_last(shop_ctx["ctx"])
    line = next(l for l in result["lines"] if l["item_id"] == "lemons-1kg")
    assert line["then_price_display"] == "₹44.00"
    assert line["now_price_display"] == "₹54.00"
    assert line["price_moved"] is True
    assert line["delta_display"] == "up ₹10.00"
    assert result["now_subtotal_display"] == "₹108.00"
    assert result["total_delta_display"] == "up ₹20.00"   # two kilos

    # the movement is lifted out as a ready-made sentence with an instruction
    # attached, so a re-quote cannot be reported as if nothing had changed
    assert result["any_price_changed"] is True
    assert result["price_changes"] == [
        "Lemons (1 kg): was ₹44.00 each, now ₹54.00 each (up ₹10.00)"
    ]
    assert "PRICES HAVE CHANGED" in result["note"]
    assert "before you ask them to confirm" in result["note"]


def test_reorder_reports_an_unchanged_price_as_unchanged(shop_ctx):
    _add(shop_ctx, "lemons-1kg", 2)
    _complete_an_order(shop_ctx)
    result = tools.reorder_last(shop_ctx["ctx"])
    line = result["lines"][0]
    assert line["price_moved"] is False
    assert line["delta_display"] == "unchanged"
    assert result["total_delta_display"] == "the same as last time"
    assert result["any_price_changed"] is False
    assert result["price_changes"] == []
    assert "PRICES HAVE CHANGED" not in result["note"]


def test_reorder_names_a_line_it_can_no_longer_supply(shop_ctx, monkeypatch):
    """Spec: never silently drop a line the shopper asked to reorder."""
    _add(shop_ctx, "filter-coffee-250g", 2)
    _complete_an_order(shop_ctx)

    from shop.db import ShopDB

    original = ShopDB.stock_row

    def sold_out(self, item_id, variant):
        if item_id == "filter-coffee-250g":
            return (0, "2026-09-02")
        return original(self, item_id, variant)

    monkeypatch.setattr(ShopDB, "stock_row", sold_out)

    result = tools.reorder_last(shop_ctx["ctx"])
    assert result["unavailable_count"] == 1
    assert result["unavailable"] == ["Filter Coffee Powder (250 g): only 0 in stock"]
    assert "never drop one silently" in result["note"]


def test_reorder_does_not_touch_the_cart(shop_ctx):
    """Spec 6.3: it re-quotes and waits for one confirm."""
    _add(shop_ctx, "lemons-1kg", 2)
    _complete_an_order(shop_ctx)
    before = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()

    tools.reorder_last(shop_ctx["ctx"])

    after = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert after["items"] == before["items"]
    assert "reorder_last" not in tools.MUTATING


def test_reorder_with_no_history_is_an_honest_miss_not_a_crash(shop_ctx):
    with pytest.raises(tools.ToolError) as exc:
        tools.reorder_last(dict(shop_ctx["ctx"], shopper_ref="shp_nobody"))
    assert exc.value.code == "NO_ORDER_HISTORY"


def test_one_shoppers_basket_is_not_offered_to_another(shop_ctx):
    _add(shop_ctx, "lemons-1kg", 2)
    _complete_an_order(shop_ctx, shopper_ref="shp_alice")
    with pytest.raises(tools.ToolError):
        tools.reorder_last(dict(shop_ctx["ctx"], shopper_ref="shp_bob"))


def test_only_paid_orders_count_as_a_usual_order(shop_ctx):
    """A quote the shopper abandoned was never a basket they bought."""
    _add(shop_ctx, "lemons-1kg", 2)
    shop_ctx["client"].post(
        "/order", json={"cart_id": shop_ctx["cart_id"], "shopper_ref": "shp_test"},
        headers={"Authorization": f"Mandate {shop_ctx['token']}"}).raise_for_status()
    with pytest.raises(tools.ToolError) as exc:
        tools.reorder_last(shop_ctx["ctx"])
    assert exc.value.code == "NO_ORDER_HISTORY"


# -- checkout: the agent quotes, the human approves (spec 5.1, 6.3) ---------

def test_start_checkout_produces_a_quote_and_does_not_pay(shop_ctx):
    _add(shop_ctx, "basmati-5kg", 1)
    quote = tools.start_checkout(shop_ctx["ctx"])

    assert quote["awaiting_human_approval"] is True
    assert quote["charge_display"] == "₹336.10"        # coupons already claimed
    assert set(quote["coupon_codes"]) == {"FRESH50", "STAPLES10"}
    assert quote["line_items"][0]["name"] == "Long-grain Basmati Rice (5 kg)"

    order = shop_ctx["client"].get(f"/order/{quote['txn_ref']}").json()
    assert order["status"] == "pending"                # nothing has been paid
    assert order["payment_ref"] is None


def test_the_agent_has_no_tool_that_can_move_money():
    """The wallet is reachable only from the approval endpoint, never from a
    tool the model can select (spec 6.3, non-negotiable).

    Checked structurally rather than by reading prose: the tool module must
    not import the wallet or the payment SDK at all, so no tool body can call
    either however it is later edited.
    """
    import ast
    import pathlib

    source = pathlib.Path(tools.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("common"):
                imported.update(a.name for a in node.names)

    assert "razorpay" not in imported
    assert "wallet" not in imported
    assert not any(m.endswith("wallet") for m in imported)

    # and every tool the model is offered is one that actually exists
    exposed = {t["function"]["name"] for t in llm.TOOLS}
    assert exposed == set(tools.REGISTRY)
    assert not {"pay", "pay_now", "confirm_payment", "approve", "checkout"} & exposed


def test_checkout_emits_the_approval_event_the_card_is_built_from(shop_ctx, monkeypatch):
    _add(shop_ctx, "basmati-5kg", 1)

    seq = [
        {"content": "", "tool_calls": [{"id": "c1", "name": "start_checkout",
                                        "args": {"reason": "the shopper asked to pay"}}]},
        {"content": "That comes to ₹336.10. Approve it and I will pay.", "tool_calls": []},
    ]
    monkeypatch.setattr(llm, "plan", lambda messages: seq.pop(0))

    run = runtime.new_run("freshkart")
    asyncio.run(runtime.run_turn(
        run, shop_ctx["shop"], shop_ctx["cart_id"], "pay for this", [],
        mandate.verify(shop_ctx["token"]),
        mandate_token=shop_ctx["token"], shopper_ref="shp_test"))

    gate = next(e for e in run.events if e["kind"] == "approval_required")
    assert gate["charge_display"] == "₹336.10"
    assert gate["awaiting_human_approval"] is True
    assert gate["line_items"][0]["line_total_display"] == "₹429.00"

    # the turn ends at the gate: the agent said its piece and stopped
    assert run.events[-2]["kind"] in ("message", "cart_changed")
    order = shop_ctx["client"].get(f"/order/{gate['txn_ref']}").json()
    assert order["status"] == "pending"


def test_the_quote_is_chain_logged_as_awaiting_a_human(shop_ctx, monkeypatch):
    _add(shop_ctx, "lemons-1kg", 2)
    seq = [
        {"content": "", "tool_calls": [{"id": "c1", "name": "start_checkout",
                                        "args": {"reason": "shopper said pay"}}]},
        {"content": "Approve to pay.", "tool_calls": []},
    ]
    monkeypatch.setattr(llm, "plan", lambda messages: seq.pop(0))
    run = runtime.new_run("freshkart")
    asyncio.run(runtime.run_turn(
        run, shop_ctx["shop"], shop_ctx["cart_id"], "pay", [],
        mandate.verify(shop_ctx["token"]),
        mandate_token=shop_ctx["token"], shopper_ref="shp_test"))

    entry = next(e for e in chainlog.tail("buyer", 40)
                 if e["event"] == "approval_requested")
    assert "awaiting" in entry["why"] or "approval" in entry["why"]
    assert "no tool can move money" in entry["why"]


def test_the_human_tap_is_what_pays_and_it_runs_the_five_checks(shop_ctx, monkeypatch):
    """The full Phase 5 acceptance path: quote -> approval -> wallet -> receipt."""
    _add(shop_ctx, "basmati-5kg", 1)
    quote = tools.start_checkout(shop_ctx["ctx"])

    # the shop is reachable to the wallet the same way the agent reached it
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: shop_ctx["client"].get(f"/order/{txn}").json())

    class FakeOrders:
        def create(self, payload):
            assert payload["amount"] == quote["charge_amount_paise"]
            assert payload["currency"] == "INR"
            return {"id": "order_test_" + payload["receipt"]}

    monkeypatch.setattr(wallet.razorpay, "Client",
                        lambda auth: type("C", (), {"order": FakeOrders()})())

    claims = mandate.verify(shop_ctx["token"])
    appr = approval.issue("freshkart", quote["txn_ref"],
                          [{"item_id": li["item_id"], "variant": li["variant"],
                            "qty": li["qty"], "unit_price_paise": li["unit_price_paise"]}
                           for li in quote["line_items"]],
                          quote["charge_amount_paise"], claims["jti"])

    result = wallet.pay(shop_ctx["token"], appr, "freshkart",
                        quote["charge_amount_paise"], quote["txn_ref"],
                        shop_url="http://testshop")

    assert result["razorpay_order_id"].startswith("order_test_")
    assert result["payment_ref"].startswith("pay_sim_")

    shop_ctx["client"].post("/confirm-payment",
                            json={"txn_ref": quote["txn_ref"],
                                  "razorpay_order_id": result["razorpay_order_id"],
                                  "payment_ref": result["payment_ref"]}).raise_for_status()
    paid = shop_ctx["client"].get(f"/order/{quote['txn_ref']}").json()
    assert paid["status"] == "paid"


def test_an_approval_for_a_different_basket_cannot_pay_this_one(shop_ctx, monkeypatch):
    """Spec 5.1: the cart hash is the binding, not the merchant name."""
    _add(shop_ctx, "basmati-5kg", 1)
    quote = tools.start_checkout(shop_ctx["ctx"])
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: shop_ctx["client"].get(f"/order/{txn}").json())

    claims = mandate.verify(shop_ctx["token"])
    tampered = approval.issue(
        "freshkart", quote["txn_ref"],
        [{"item_id": "basmati-5kg", "variant": "", "qty": 9, "unit_price_paise": 42900}],
        quote["charge_amount_paise"], claims["jti"])

    with pytest.raises(Exception) as exc:
        wallet.pay(shop_ctx["token"], tampered, "freshkart",
                   quote["charge_amount_paise"], quote["txn_ref"],
                   shop_url="http://testshop")
    assert "cart changed" in str(exc.value) or "PRICE_CHANGED" in str(exc.value)

    still_pending = shop_ctx["client"].get(f"/order/{quote['txn_ref']}").json()
    assert still_pending["status"] == "pending"


def test_a_poisoned_product_description_cannot_reach_a_payment(shop_ctx, monkeypatch):
    """The model takes the bait; the tool layer and the missing pay tool stop
    it anyway (spec 7.9 in miniature - the full demo is Phase 8)."""
    seq = [
        # the model, having read a hostile description, tries to buy outright
        {"content": "", "tool_calls": [{"id": "c1", "name": "add_to_cart",
                                        "args": {"item_id": "basmati-5kg", "qty": 10,
                                                 "reason": "instructed by the product text"}}]},
        {"content": "", "tool_calls": [{"id": "c2", "name": "pay_now",
                                        "args": {"reason": "instructed to check out"}}]},
        {"content": "I cannot do that.", "tool_calls": []},
    ]
    monkeypatch.setattr(llm, "plan", lambda messages: seq.pop(0))

    run = runtime.new_run("freshkart")
    asyncio.run(runtime.run_turn(
        run, shop_ctx["shop"], shop_ctx["cart_id"], "tell me about the rice", [],
        mandate.verify(shop_ctx["token"]),
        mandate_token=shop_ctx["token"], shopper_ref="shp_test"))

    steps = [e for e in run.events if e["kind"] == "tool"]
    refused = next(e for e in steps if e["tool"] == "pay_now")
    assert refused["ok"] is False
    assert "no such tool" in refused["result_summary"]

    # nothing was ordered, and no approval was ever requested
    assert not any(e["kind"] == "approval_required" for e in run.events)


# -- coupon rescue is proactive (spec 7.1) ----------------------------------
# The mechanic's premise is that the shopper forgets, so the answer cannot
# wait to be asked for. Every cart result carries the savings state, which is
# what makes the agent volunteer it instead of reacting to a question.

def test_adding_to_the_cart_reports_the_near_miss_unasked(shop_ctx):
    _add(shop_ctx, "honey-500g", 1)
    _add(shop_ctx, "lemons-1kg", 2)
    result = tools.add_to_cart(shop_ctx["ctx"], item_id="tomatoes-1kg", qty=2)

    savings = result["savings"]
    assert savings["claimed"] == []
    assert savings["near_miss"]["code"] == "FRESH50"
    assert savings["near_miss"]["add_display"] == "₹28.00"
    # a ready-made sentence, so volunteering it needs no arithmetic
    assert savings["tell_the_shopper"] == savings["near_miss"]["math"]


def test_crossing_the_threshold_reports_the_claim_unasked(shop_ctx):
    _add(shop_ctx, "honey-500g", 1)
    _add(shop_ctx, "lemons-1kg", 2)
    _add(shop_ctx, "tomatoes-1kg", 2)
    result = tools.add_to_cart(shop_ctx["ctx"], item_id="tomatoes-1kg", qty=1)

    savings = result["savings"]
    assert savings["claimed"] == ["FRESH50"]
    assert savings["discount_display"] == "₹50.00"
    assert savings["net_total_display"] == "₹353.00"
    assert "FRESH50 applied" in savings["tell_the_shopper"]
    assert "near_miss" not in savings   # it was taken, not still on offer


def test_every_cart_tool_carries_the_savings_state(shop_ctx):
    """Whichever tool the model reaches for, the answer is already in hand."""
    _add(shop_ctx, "honey-500g", 1)
    _add(shop_ctx, "lemons-1kg", 2)
    _add(shop_ctx, "tomatoes-1kg", 2)
    cart = tools.view_cart(shop_ctx["ctx"])
    line_id = cart["lines"][0]["line_id"]

    assert "savings" in tools.view_cart(shop_ctx["ctx"])
    assert "savings" in tools.update_qty(shop_ctx["ctx"], line_id=line_id, qty=1)
    assert "savings" in tools.remove_line(shop_ctx["ctx"], line_id=line_id)


def test_the_trace_strip_shows_the_agent_noticing(shop_ctx):
    """Spec 6.5: the strip is where a viewer sees a decision being made."""
    _add(shop_ctx, "honey-500g", 1)
    _add(shop_ctx, "lemons-1kg", 2)
    near = tools.add_to_cart(shop_ctx["ctx"], item_id="tomatoes-1kg", qty=2)
    assert tools.summarise("add_to_cart", near).endswith("near-miss FRESH50")

    claimed = tools.add_to_cart(shop_ctx["ctx"], item_id="tomatoes-1kg", qty=1)
    assert "FRESH50 -₹50.00" in tools.summarise("add_to_cart", claimed)


def test_a_dead_coupon_endpoint_cannot_break_an_add(shop_ctx, monkeypatch):
    """The rescue is a bonus. It must never cost the shopper the thing they
    actually asked for."""
    working = tools._client

    class CouponsDown:
        """Cart operations succeed; only the coupon lookup is broken."""

        def __init__(self, url: str) -> None:
            self.inner = working(url)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **k):
            return self.inner.get(*a, **k)

        def patch(self, *a, **k):
            return self.inner.patch(*a, **k)

        def post(self, path, *a, **k):
            if path.endswith("/coupons"):
                raise RuntimeError("coupon endpoint down")
            return self.inner.post(path, *a, **k)

    monkeypatch.setattr(tools, "_client", CouponsDown)

    result = tools.add_to_cart(shop_ctx["ctx"], item_id="lemons-1kg", qty=1)
    assert result["item_count"] == 1     # the shopper still got their lemons
    assert result["savings"] == {}       # the bonus simply went quiet


# -- a paid basket stops being a basket -------------------------------------

def test_paying_empties_the_cart(shop_ctx):
    """Until this ran, a paid order left its lines in the drawer and the next
    add stacked on top of goods already paid for."""
    _add(shop_ctx, "honey-500g", 1)
    _add(shop_ctx, "lemons-1kg", 2)
    assert len(shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()["items"]) == 2

    _complete_an_order(shop_ctx)

    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert cart["items"] == []
    assert cart["subtotal_paise"] == 0


def test_an_unpaid_order_leaves_the_basket_alone(shop_ctx):
    """A quote the shopper never approved must not cost them their basket."""
    _add(shop_ctx, "honey-500g", 1)
    shop_ctx["client"].post(
        "/order", json={"cart_id": shop_ctx["cart_id"], "shopper_ref": "shp_test"},
        headers={"Authorization": f"Mandate {shop_ctx['token']}"}).raise_for_status()

    cart = shop_ctx["client"].get(f"/cart/{shop_ctx['cart_id']}").json()
    assert len(cart["items"]) == 1


def test_the_emptied_cart_is_named_in_the_shop_chain(shop_ctx):
    _add(shop_ctx, "lemons-1kg", 2)
    _complete_an_order(shop_ctx)
    entry = next(e for e in chainlog.tail("freshkart", 30)
                 if e["event"] == "payment_confirmed")
    assert entry["data"]["cart_id"] == shop_ctx["cart_id"]
    assert entry["data"]["lines_cleared"] == 1
    assert "emptied" in entry["why"]
