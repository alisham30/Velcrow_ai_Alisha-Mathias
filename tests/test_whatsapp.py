"""WhatsApp outreach: words to the shopper, money still through one door.

What these tests actually pin down:
- the transport falls back to an honest outbox when no credentials exist,
  and only common/whatsapp.py may speak to graph.facebook.com (guard);
- an unsigned or forged webhook does NOTHING; a replayed one acts once;
- an Approve tap pays through the same place_order -> approval -> wallet
  five-checks path as every other dialect, capped at the amount the
  message quoted;
- a tap from the wrong number is refused; a dearer basket is re-quoted,
  never charged; a decline is final and unargued-with;
- an abandoned cart is reminded about exactly once.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common import wallet, whatsapp
from agent import outreach

REPO = Path(__file__).resolve().parent.parent
WA_ID = "919821158848"              # the shopper's WhatsApp number
CONTACT_KEY = "phone:9821158848"    # what the demand ledger holds for them
APP_SECRET = "test-app-secret"


# -- plumbing -----------------------------------------------------------------

class FakeRazorpayClient:
    created: list[dict[str, Any]] = []

    def __init__(self, auth: tuple[str, str]) -> None:
        self.order = self

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        FakeRazorpayClient.created.append(payload)
        return {"id": f"order_FAKE{len(FakeRazorpayClient.created):03d}", **payload}


class Routed:
    """Stands in for httpx.Client: routes outreach's shop calls to the
    in-process TestClient."""

    def __init__(self, client, base_url: str = "", timeout: int = 0) -> None:
        self._c = client

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path, **kw):
        return self._c.get(path, **kw)

    def post(self, path, **kw):
        return self._c.post(path, **kw)


@pytest.fixture
def wa(env, freshkart, monkeypatch):
    """Everything wired: unconfigured transport (outbox mode), webhook secret
    set, shop + wallet routed in-process."""
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_ID", raising=False)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-verify")
    monkeypatch.setattr(outreach, "httpx",
                        SimpleNamespace(Client=lambda base_url="", timeout=0:
                                        Routed(freshkart)))
    FakeRazorpayClient.created = []
    monkeypatch.setattr(wallet, "razorpay", SimpleNamespace(Client=FakeRazorpayClient))
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: freshkart.get(f"/order/{txn}").json())
    # chat turns run on worker threads in production; tests must opt in
    # explicitly (inline_spawn) or a text message quietly races the assertions
    monkeypatch.setattr(outreach, "_spawn", lambda fn: None)
    return freshkart


def _signed(payload: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode()
    return raw, "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _tap_payload(button_id: str, wamid: str = "wamid.tap1") -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": WA_ID, "profile": {"name": "Zoya"}}],
        "messages": [{"from": WA_ID, "id": wamid, "type": "interactive",
                      "interactive": {"type": "button_reply",
                                      "button_reply": {"id": button_id, "title": "x"}}}],
    }}]}]}


def _restock_offer(wa, qty: int = 2, unit: int | None = None) -> str:
    price = unit if unit is not None else wa.get("/product/lemons-1kg").json()["price_paise"]
    out = outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "res_test1", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": qty,
        "unit_price_paise": price, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": True, "shopper_ref": "shp_1"})
    assert out["offer_id"]
    return out["offer_id"]


def _offer_row(offer_id: str) -> dict[str, Any]:
    return next(o for o in outreach.offers(100) if o["offer_id"] == offer_id)


# -- transport ----------------------------------------------------------------

def test_unconfigured_transport_lands_in_the_outbox_not_the_void(wa):
    result = whatsapp.send_text(WA_ID, "hello")
    assert result["mode"] == "outbox"


def test_only_the_transport_module_speaks_to_meta():
    skip = {".venv", "node_modules", "__pycache__", ".git", "tests"}
    offenders = [p for p in REPO.rglob("*.py")
                 if not (set(p.relative_to(REPO).parts) & skip)
                 and p.name != "whatsapp.py"
                 and "graph.facebook.com" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == [], f"graph.facebook.com outside common/whatsapp.py: {offenders}"


# -- webhook gate -------------------------------------------------------------

def test_a_forged_webhook_does_nothing_at_all(wa):
    raw = json.dumps(_tap_payload("apr_whatever")).encode()
    from common import errors
    with pytest.raises(errors.VelcrowError):
        outreach.handle_webhook(raw, "sha256=" + "0" * 64)
    assert outreach.outbox(10) == []          # no message went anywhere


def test_verify_challenge_echoes_only_on_the_right_token(wa):
    assert outreach.verify_challenge("subscribe", "test-verify", "12345") == "12345"
    from common import errors
    with pytest.raises(errors.VelcrowError):
        outreach.verify_challenge("subscribe", "wrong", "12345")


def test_first_text_binds_the_number_and_greets_once(wa):
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": WA_ID, "profile": {"name": "Zoya"}}],
        "messages": [{"from": WA_ID, "id": "wamid.hi1", "type": "text",
                      "text": {"body": "hi"}}]}}]}]}
    raw, sig = _signed(payload)
    outreach.handle_webhook(raw, sig)
    greetings = [m for m in outreach.outbox(10) if m["kind"] == "greeting"]
    assert len(greetings) == 1
    # replay of the same wamid acts zero more times; a NEW text greets zero
    # more times (the binding already exists)
    outreach.handle_webhook(raw, sig)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.hi2"
    raw2, sig2 = _signed(payload)
    outreach.handle_webhook(raw2, sig2)
    assert len([m for m in outreach.outbox(20) if m["kind"] == "greeting"]) == 1


# -- restock offer ------------------------------------------------------------

def test_restock_offer_is_composed_once_per_reservation(wa):
    _restock_offer(wa)
    sent = [m for m in outreach.outbox(10) if m["kind"] == "restock_offer"]
    assert len(sent) == 1
    assert "you authorise exactly" in sent[0]["body"]
    # same reservation again: say once
    out2 = outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "res_test1", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": 2,
        "unit_price_paise": 100, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": True})
    assert out2["messaged"] is False
    assert len([m for m in outreach.outbox(10) if m["kind"] == "restock_offer"]) == 1


def test_a_shopper_without_a_phone_is_not_messaged(wa):
    out = outreach.on_restock_offer({
        "contact_key": "email:zoya@example.com", "res_id": "res_mail", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": 1,
        "unit_price_paise": 100, "shop_id": "freshkart", "shop_url": "http://testshop"})
    assert out["messaged"] is False
    assert outreach.outbox(10) == []


# -- the tap ------------------------------------------------------------------

def test_approve_tap_pays_through_the_wallet_and_confirms(wa):
    offer_id = _restock_offer(wa, qty=2)
    quoted = _offer_row(offer_id)["quoted_paise"]

    raw, sig = _signed(_tap_payload(f"apr_{offer_id}"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]

    assert result["paid"] is True
    assert result["amount_paise"] <= quoted            # never more than the message said
    assert len(FakeRazorpayClient.created) == 1        # money moved exactly once, via wallet
    order = wa.get(f"/order/{result['txn_ref']}").json()
    assert order["status"] == "paid"
    row = _offer_row(offer_id)
    assert row["status"] == "approved" and row["txn_ref"] == result["txn_ref"]
    receipts = [m for m in outreach.outbox(10) if m["kind"] == "receipt"]
    assert len(receipts) == 1 and result["txn_ref"] in receipts[0]["body"]


def test_a_tap_from_someone_elses_number_is_refused(wa):
    offer_id = _restock_offer(wa)
    payload = _tap_payload(f"apr_{offer_id}")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "917000000001"
    raw, sig = _signed(payload)
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result["why"] == "sender mismatch"
    assert _offer_row(offer_id)["status"] == "pending"     # still the shopper's to decide
    assert FakeRazorpayClient.created == []


def test_a_dearer_basket_is_requoted_never_charged(wa):
    # The message quoted one paisa; the real shelf price is far above it, so
    # the tap must NOT pay and the shopper must get the true number to decide on.
    offer_id = _restock_offer(wa, qty=1, unit=1)
    raw, sig = _signed(_tap_payload(f"apr_{offer_id}"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result.get("paid") is None
    assert FakeRazorpayClient.created == []
    assert _offer_row(offer_id)["status"] == "superseded"
    new_id = result["requoted_as"]
    fresh = _offer_row(new_id)
    assert fresh["status"] == "pending" and fresh["quoted_paise"] > 1
    assert any(m["kind"] == "requote" for m in outreach.outbox(10))


def test_decline_is_final_and_double_taps_do_nothing(wa):
    offer_id = _restock_offer(wa)
    raw, sig = _signed(_tap_payload(f"dec_{offer_id}", wamid="wamid.d1"))
    assert outreach.handle_webhook(raw, sig)["handled"][0]["declined"] is True
    assert _offer_row(offer_id)["status"] == "declined"
    # an approve after the decline finds nothing pending; nothing is charged
    raw2, sig2 = _signed(_tap_payload(f"apr_{offer_id}", wamid="wamid.d2"))
    assert outreach.handle_webhook(raw2, sig2)["handled"][0]["why"] == "not pending"
    assert FakeRazorpayClient.created == []


def test_a_stale_offer_expires_instead_of_charging(wa, monkeypatch):
    offer_id = _restock_offer(wa)
    monkeypatch.setattr(outreach, "RESTOCK_OFFER_TTL", -1)
    raw, sig = _signed(_tap_payload(f"apr_{offer_id}"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result["why"] == "expired"
    assert FakeRazorpayClient.created == []
    assert _offer_row(offer_id)["status"] == "expired"


# -- abandoned carts ----------------------------------------------------------

def _quiet_cart(wa) -> str:
    cart = wa.post("/cart").json()["cart_id"]
    r = wa.patch(f"/cart/{cart}", json={"op": "add", "item_id": "lemons-1kg",
                                        "variant": "", "qty": 2})
    assert r.status_code == 200, r.text
    outreach.record_cart_activity("freshkart", "http://testshop", cart, CONTACT_KEY, "shp_1")
    return cart


def test_an_abandoned_cart_is_reminded_exactly_once(wa):
    _quiet_cart(wa)
    later = time.time() + outreach.ABANDON_AFTER_SECONDS + 5
    sent = outreach.sweep_abandoned(now=later)
    assert len(sent) == 1
    body = [m for m in outreach.outbox(10) if m["kind"] == "cart_reminder"][0]["body"]
    assert "Total:" in body and "won't ask again" in body
    assert outreach.sweep_abandoned(now=later + 10) == []      # say once


def test_new_activity_resets_the_reminder_but_an_empty_cart_never_reminds(wa):
    cart = _quiet_cart(wa)
    later = time.time() + outreach.ABANDON_AFTER_SECONDS + 5
    assert len(outreach.sweep_abandoned(now=later)) == 1
    # they came back and emptied it: activity recorded, cart now empty
    line = wa.get(f"/cart/{cart}").json()["items"][0]["line_id"]
    wa.patch(f"/cart/{cart}", json={"op": "remove", "line_id": line})
    outreach.record_cart_activity("freshkart", "http://testshop", cart, CONTACT_KEY, "shp_1")
    assert outreach.sweep_abandoned(now=later + outreach.ABANDON_AFTER_SECONDS + 10) == []


# -- WhatsApp OTP login -------------------------------------------------------

def _latest_code() -> str:
    body = [m for m in outreach.outbox(10) if m["kind"] == "login_code"][0]["body"]
    return body.split(" ", 1)[0]


def test_login_code_is_sent_verified_and_single_use(wa):
    out = outreach.start_login("+91 98211 58848")
    assert out["mode"] == "outbox"           # honest: no transport configured in tests
    code = _latest_code()
    assert len(code) == 6 and code.isdigit()
    got = outreach.verify_login("9821158848", code)   # different spelling, same person
    assert got == {"verified": True, "contact_key": CONTACT_KEY}
    from common import errors
    with pytest.raises(errors.VelcrowError):          # a code is spendable exactly once
        outreach.verify_login("9821158848", code)


def test_three_wrong_attempts_burn_the_code(wa):
    outreach.start_login(WA_ID)
    code = _latest_code()
    from common import errors
    for _ in range(3):
        with pytest.raises(errors.VelcrowError):
            outreach.verify_login(WA_ID, "000000" if code != "000000" else "111111")
    with pytest.raises(errors.VelcrowError):          # even the RIGHT code is dead now
        outreach.verify_login(WA_ID, code)


def test_an_expired_code_is_refused(wa, monkeypatch):
    monkeypatch.setattr(outreach, "LOGIN_CODE_TTL", -1)
    outreach.start_login(WA_ID)
    from common import errors
    with pytest.raises(errors.MandateExpired):
        outreach.verify_login(WA_ID, _latest_code())


def test_code_requests_are_rate_limited(wa):
    for _ in range(outreach.LOGIN_RESEND_MAX):
        outreach.start_login(WA_ID)
    from common import errors
    with pytest.raises(errors.BadRequest):
        outreach.start_login(WA_ID)


def test_email_cannot_whatsapp_login(wa):
    from common import errors
    with pytest.raises(errors.BadRequest):
        outreach.start_login("zoya@example.com")


def test_approving_the_cart_reminder_pays_the_real_cart(wa):
    _quiet_cart(wa)
    sent = outreach.sweep_abandoned(now=time.time() + outreach.ABANDON_AFTER_SECONDS + 5)
    offer_id = sent[0]["offer_id"]
    raw, sig = _signed(_tap_payload(f"apr_{offer_id}"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result["paid"] is True
    assert wa.get(f"/order/{result['txn_ref']}").json()["status"] == "paid"
    assert len(FakeRazorpayClient.created) == 1


def test_four_refusals_one_person_one_item_is_one_message(wa):
    """A restock fans out one callback per ledger row; a shopper refused three
    times for the same lemons must still get exactly ONE WhatsApp ping."""
    for i, qty in enumerate((3, 3, 3, 1)):
        outreach.on_restock_offer({
            "contact_key": CONTACT_KEY, "res_id": f"demand_{i}", "item_id": "lemons-1kg",
            "product_name": "Lemons 1kg", "variant": "", "qty": qty,
            "unit_price_paise": 4400, "shop_id": "freshkart",
            "shop_url": "http://testshop", "held": False, "shopper_ref": "shp_1"})
    assert len([m for m in outreach.outbox(20) if m["kind"] == "restock_offer"]) == 1
    # a DIFFERENT item for the same person is a new thing to say
    outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "demand_9", "item_id": "honey-500g",
        "product_name": "Honey", "variant": "", "qty": 1,
        "unit_price_paise": 24900, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": False, "shopper_ref": "shp_1"})
    assert len([m for m in outreach.outbox(20) if m["kind"] == "restock_offer"]) == 2


def test_restock_completes_the_basket_instead_of_peddling_the_unit(wa, buyer_mandate):
    """Found live: wanting 6 with 5 on the shelf, the restock message quoted
    the lone returned unit and ignored the basket. Now the refused units go
    BACK INTO the cart (the original ask; no money moved) and the offer quotes
    the whole basket - and approving pays for all of it."""
    stock = wa.get("/product/lemons-1kg").json()["stock"]
    assert stock >= 2
    cart = wa.post("/cart").json()["cart_id"]
    split = wa.post(f"/cart/{cart}/fulfil",
                    headers={"Authorization": f"Mandate {buyer_mandate}"},
                    json={"item_id": "lemons-1kg", "variant": "", "qty": stock + 2,
                          "mode": "add", "shopper_ref": "shp_1",
                          "contact_ref": WA_ID}).json()
    assert split["added"] == stock and split["shortfall"] == 2
    wa.post("/admin/restock", json={"item_id": "lemons-1kg", "variant": "", "qty": 6})

    out = outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "demand_x", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": 2,
        "unit_price_paise": 4400, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": False, "shopper_ref": "shp_1",
        "cart_id": cart})
    assert out.get("completed_basket") is True

    # the cart itself was made whole - the shopper's original ask
    lines = wa.get(f"/cart/{cart}").json()["items"]
    assert sum(l["qty"] for l in lines) == stock + 2
    msg = [m for m in outreach.outbox(10) if m["kind"] == "basket_completed"][0]["body"]
    assert "whole basket" in msg

    # a SECOND restock does not stuff the basket again
    again = outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "demand_y", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": 2,
        "unit_price_paise": 4400, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": False, "shopper_ref": "shp_1",
        "cart_id": cart})
    assert again["messaged"] is False
    assert sum(l["qty"] for l in wa.get(f"/cart/{cart}").json()["items"]) == stock + 2

    # approving pays for the WHOLE basket through the wallet
    offer_id = out["offer_id"]
    raw, sig = _signed(_tap_payload(f"apr_{offer_id}"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result["paid"] is True
    order = wa.get(f"/order/{result['txn_ref']}").json()
    assert sum(l["qty"] for l in order["line_items"]) == stock + 2


def test_a_gone_basket_falls_back_to_the_unit_offer(wa):
    out = outreach.on_restock_offer({
        "contact_key": CONTACT_KEY, "res_id": "demand_z", "item_id": "lemons-1kg",
        "product_name": "Lemons 1kg", "variant": "", "qty": 1,
        "unit_price_paise": 4400, "shop_id": "freshkart",
        "shop_url": "http://testshop", "held": False, "shopper_ref": "shp_1",
        "cart_id": "cart_nonexistent"})
    assert "completed_basket" not in out
    assert [m for m in outreach.outbox(10) if m["kind"] == "restock_offer"]


# -- conversational shopping over WhatsApp ------------------------------------

def _text_payload(text: str, wamid: str) -> dict[str, Any]:
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": WA_ID, "profile": {"name": "Zoya"}}],
        "messages": [{"from": WA_ID, "id": wamid, "type": "text",
                      "text": {"body": text}}]}}]}]}


@pytest.fixture
def inline_spawn(wa, monkeypatch):
    """Run the chat worker on this thread so tests see its effects."""
    monkeypatch.setattr(outreach, "_spawn", lambda fn: fn())


def test_a_text_runs_the_agent_and_replies(wa, inline_spawn, monkeypatch):
    monkeypatch.setattr(outreach, "_agent_turn",
                        lambda chat, text, key: [
                            {"kind": "message",
                             "text": "Added 2 kg lemons - your basket is ₹88.00."}])
    raw, sig = _signed(_text_payload("add 2 kg lemons", "wamid.c1"))
    result = outreach.handle_webhook(raw, sig)["handled"][0]
    assert result["kind"] == "chat_turn_started"
    replies = [m for m in outreach.outbox(10) if m["kind"] == "chat_reply"]
    assert replies and "lemons" in replies[0]["body"]
    # the conversation remembers the exchange for the next turn
    with outreach._db() as c:
        row = c.execute("SELECT * FROM wa_chats WHERE contact_key = ?",
                        (CONTACT_KEY,)).fetchone()
    assert row is not None
    hist = json.loads(row["history"])
    assert hist[-1]["role"] == "assistant" and "lemons" in hist[-1]["content"]


def test_the_models_own_quote_becomes_the_approve_button_and_pays(
        wa, inline_spawn, monkeypatch, buyer_mandate):
    """The MODEL decides to check out (its start_checkout tool places the
    order); this surface only turns that decision into buttons - and the tap
    pays that exact order through the wallet."""
    def fake_brain(chat, text, key):
        cart = chat["cart_id"]
        wa.post(f"/cart/{cart}/fulfil",
                headers={"Authorization": f"Mandate {buyer_mandate}"},
                json={"item_id": "lemons-1kg", "variant": "", "qty": 2, "mode": "add"})
        placed = wa.post("/order", headers={"Authorization": f"Mandate {buyer_mandate}",
                                            "Idempotency-Key": "wa-chat-test"},
                         json={"cart_id": cart, "assisted": True}).json()
        return [{"kind": "message", "text": "Two lemons, ready to pay."},
                {"kind": "approval_required", "txn_ref": placed["txn_ref"],
                 "charge_amount_paise": placed["charge_amount"],
                 "line_items": placed["line_items"]}]

    monkeypatch.setattr(outreach, "_agent_turn", fake_brain)
    raw, sig = _signed(_text_payload("2 lemons and checkout", "wamid.c2"))
    outreach.handle_webhook(raw, sig)

    quotes = [m for m in outreach.outbox(10) if m["kind"] == "quote"]
    assert len(quotes) == 1 and "Tap Approve to pay exactly" in quotes[0]["body"]
    offer = next(o for o in outreach.offers(10) if o["kind"] == "quote")
    assert offer["status"] == "pending" and offer["txn_ref"]

    raw2, sig2 = _signed(_tap_payload(f"apr_{offer['offer_id']}", wamid="wamid.c3"))
    result = outreach.handle_webhook(raw2, sig2)["handled"][0]
    assert result["paid"] is True and result["txn_ref"] == offer["txn_ref"]
    assert wa.get(f"/order/{offer['txn_ref']}").json()["status"] == "paid"
    assert len(FakeRazorpayClient.created) == 1


def test_a_lapsed_price_lock_refuses_honestly(wa, inline_spawn, monkeypatch, buyer_mandate):
    import shop.db as shopdb

    def fake_brain(chat, text, key):
        monkeypatch.setattr(shopdb, "PRICE_LOCK_SECONDS", -1)   # lock lapses instantly
        cart = chat["cart_id"]
        wa.post(f"/cart/{cart}/fulfil",
                headers={"Authorization": f"Mandate {buyer_mandate}"},
                json={"item_id": "lemons-1kg", "variant": "", "qty": 1, "mode": "add"})
        placed = wa.post("/order", headers={"Authorization": f"Mandate {buyer_mandate}",
                                            "Idempotency-Key": "wa-chat-exp"},
                         json={"cart_id": cart, "assisted": True}).json()
        return [{"kind": "approval_required", "txn_ref": placed["txn_ref"],
                 "charge_amount_paise": placed["charge_amount"],
                 "line_items": placed["line_items"]}]

    monkeypatch.setattr(outreach, "_agent_turn", fake_brain)
    raw, sig = _signed(_text_payload("1 lemon checkout", "wamid.c4"))
    outreach.handle_webhook(raw, sig)
    offer = next(o for o in outreach.offers(10) if o["kind"] == "quote")

    raw2, sig2 = _signed(_tap_payload(f"apr_{offer['offer_id']}", wamid="wamid.c5"))
    result = outreach.handle_webhook(raw2, sig2)["handled"][0]
    assert result["why"] == "quote expired"
    assert FakeRazorpayClient.created == []
    assert any(m["kind"] == "expired" for m in outreach.outbox(10))


def test_the_model_routes_the_message_to_a_shop(wa, inline_spawn, monkeypatch):
    """No keyword list decides the shop - the model does (with a fixed answer
    space and a deterministic fallback). A message it routes elsewhere gets a
    fresh conversation at that shop."""
    routed = []

    def fake_router(text, current):
        routed.append((text, current))
        return "apparel"

    monkeypatch.setattr(outreach, "_route_shop", fake_router)
    seen = {}
    monkeypatch.setattr(outreach, "_agent_turn",
                        lambda chat, text, key: seen.update(chat) or
                        [{"kind": "message", "text": "Three dupattas coming up."}])
    raw, sig = _signed(_text_payload("3 chanderi dupattas", "wamid.c6"))
    outreach.handle_webhook(raw, sig)
    assert routed and routed[0][0] == "3 chanderi dupattas"
    assert seen["shop_key"] == "apparel" and seen["shop_id"] == "loomcraft"


def test_a_router_outage_falls_back_deterministically(wa, inline_spawn, monkeypatch):
    """The routing judgment may come from a model; being DOWN may not route
    the shopper into a void. With no API key the fallback scorer answers."""
    seen = {}
    monkeypatch.setattr(outreach, "_agent_turn",
                        lambda chat, text, key: seen.update(chat) or
                        [{"kind": "message", "text": "ok"}])
    raw, sig = _signed(_text_payload("1 kg lemons please", "wamid.c7"))
    outreach.handle_webhook(raw, sig)     # no OPENAI key in tests -> fallback
    assert seen["shop_key"] == "grocery"


def test_order_history_names_the_door_each_order_came_through(wa):
    """The Orders page's contract: every paid order carries its source -
    a WhatsApp purchase says whatsapp, a storefront one says storefront."""
    offer_id = _restock_offer(wa, qty=2)
    raw, sig = _signed(_tap_payload(f"apr_{offer_id}", wamid="wamid.h1"))
    assert outreach.handle_webhook(raw, sig)["handled"][0]["paid"] is True

    hist = wa.get(f"/orders/history?contact_key={CONTACT_KEY}").json()
    assert hist["orders"], "the WhatsApp purchase must appear"
    assert hist["orders"][0]["source"] == "whatsapp"
    assert hist["orders"][0]["line_items"][0]["name"]      # human names, not ids


# -- cross-shop goal shopping --------------------------------------------------

def _goal_router(monkeypatch):
    monkeypatch.setattr(outreach, "_route_message",
                        lambda text, current: {"mode": "goal", "shop": "grocery"})


def test_a_goal_ranks_options_across_shops_and_a_number_buys(
        wa, inline_spawn, monkeypatch):
    """'find me X under Y' -> buyer machinery -> ranked list -> reply '1' ->
    firm quote -> Approve tap -> the buyer agent's own approve path pays."""
    _goal_router(monkeypatch)
    calls = []

    def fake_buyer(method, path, body=None):
        calls.append((method, path, body))
        if path == "/buyer/run":
            return 201, {"run_id": "buy_test1", "status": "options",
                         "rules": {"budget_display": "₹1,500.00"},
                         "messages": [],
                         "options": [
                             {"option_id": "opt_a", "name": "Indigo Cotton Kurti (M)",
                              "shop_name": "Loomcraft", "price_display": "₹1,499.00",
                              "selectable": True},
                             {"option_id": "opt_b", "name": "Silk Kurti",
                              "shop_name": "Loomcraft", "price_display": "₹2,199.00",
                              "selectable": False, "breaks_rules": ["over budget"]}]}
        if path.endswith("/choose"):
            return 200, {"status": "awaiting_approval", "messages": [],
                         "quote": {"txn_ref": "txn_goal1", "charge_amount": 149_900,
                                   "charge_display": "₹1,499.00", "shop_id": "loomcraft",
                                   "shop_name": "Loomcraft", "shop_url": "http://testshop"}}
        if path.endswith("/approve"):
            return 200, {"status": "paid", "receipt": {"txn_ref": "txn_goal1"},
                         "messages": [{"kind": "receipt",
                                       "text": "Paid ₹1,499.00 to Loomcraft."}]}
        raise AssertionError(path)

    monkeypatch.setattr(outreach, "_buyer_call", fake_buyer)

    raw, sig = _signed(_text_payload("find me a cotton kurti under 1500", "wamid.g1"))
    outreach.handle_webhook(raw, sig)
    listing = [m for m in outreach.outbox(10) if m["kind"] == "goal_options"][0]["body"]
    assert "1. Indigo Cotton Kurti (M) - ₹1,499.00 at Loomcraft" in listing
    assert "can't buy: over budget" in listing            # refused, with the reason
    assert "enforced by the wallet" in listing

    raw2, sig2 = _signed(_text_payload("1", "wamid.g2"))
    outreach.handle_webhook(raw2, sig2)
    quote = [m for m in outreach.outbox(10) if m["kind"] == "goal_quote"][0]["body"]
    assert "₹1,499.00" in quote
    offer = next(o for o in outreach.offers(10) if o["kind"] == "goal")

    raw3, sig3 = _signed(_tap_payload(f"apr_{offer['offer_id']}", wamid="wamid.g3"))
    result = outreach.handle_webhook(raw3, sig3)["handled"][0]
    assert result["paid"] is True
    assert ("POST", "/buyer/run/buy_test1/approve", {}) == calls[-1]
    assert any(m["kind"] == "receipt" and "Loomcraft" in m["body"]
               for m in outreach.outbox(10))


def test_a_goal_without_a_budget_asks_and_the_reply_completes_it(
        wa, inline_spawn, monkeypatch):
    """Missing budget -> the agent ASKS instead of guessing; the next message
    is treated as the answer, not as a new conversation."""
    _goal_router(monkeypatch)
    goals = []

    def fake_buyer(method, path, body=None):
        if path == "/buyer/run":
            goals.append(body["goal"])
            if "under" not in body["goal"] and "1500" not in body["goal"]:
                return 201, {"run_id": "buy_ask", "status": "needs_clarification",
                             "messages": [{"kind": "ask",
                                           "text": "Before I go looking I need a budget."}],
                             "options": []}
            return 201, {"run_id": "buy_ok", "status": "options", "rules": {},
                         "messages": [],
                         "options": [{"option_id": "o1", "name": "Kurti",
                                      "shop_name": "Loomcraft",
                                      "price_display": "₹1,499.00", "selectable": True}]}
        raise AssertionError(path)

    monkeypatch.setattr(outreach, "_buyer_call", fake_buyer)
    raw, sig = _signed(_text_payload("find me a cotton kurti", "wamid.g4"))
    outreach.handle_webhook(raw, sig)
    assert any(m["kind"] == "goal_ask" and "budget" in m["body"]
               for m in outreach.outbox(10))

    raw2, sig2 = _signed(_text_payload("under 1500", "wamid.g5"))
    outreach.handle_webhook(raw2, sig2)
    assert goals[-1] == "find me a cotton kurti under 1500"   # combined, not restarted
    assert any(m["kind"] == "goal_options" for m in outreach.outbox(10))


def test_the_router_is_told_what_each_shop_actually_sells(wa, inline_spawn, monkeypatch):
    """Found live: 'are there any cushions' stayed at the grocer, because the
    router's menu had names and categories but no goods. The menu now carries
    each shop's own catalog vocabulary."""
    from agent import orchestrator

    outreach._MENU_CACHE.update(ts=0.0, menu={})
    seen_menus = []

    def spy_route(text, current, shops, fallback):
        seen_menus.append(shops)
        return {"mode": "shop", "shop": "grocery"}

    monkeypatch.setattr(orchestrator, "route", spy_route)
    monkeypatch.setattr(outreach, "_agent_turn",
                        lambda chat, text, key: [{"kind": "message", "text": "ok"}])
    raw, sig = _signed(_text_payload("are there any cushions", "wamid.m1"))
    outreach.handle_webhook(raw, sig)
    assert seen_menus, "router was never consulted"
    menu = seen_menus[0]
    # every entry names goods, not just a category (Routed serves freshkart's
    # catalog for each URL in tests - the point is the vocabulary is THERE)
    assert all(": sells " in v for v in menu.values()), menu
    assert any("citrus" in v or "staples" in v for v in menu.values())
