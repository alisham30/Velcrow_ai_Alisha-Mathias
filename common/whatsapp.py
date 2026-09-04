"""WhatsApp Cloud API transport, and nothing else (spec 16 addendum).

This module carries WORDS to a phone: cart summaries, restock news, approval
buttons, receipts. It never carries money and never sees a card, a UPI id or
a token of value - authorisation happens in the payload the buttons reference,
and money still moves only through common/wallet.py.

Test-mode discipline, same as Razorpay: the Meta app's TEST number can only
message the handful of numbers its owner verified, so a bug here can spam
nobody. When the WhatsApp env vars are absent every send falls back to mode
"outbox" - recorded, displayable, honestly NOT delivered - so the rest of the
system (tests, CI, run_all without credentials) never depends on Meta being
reachable.

No other module may talk to graph.facebook.com (test-enforced, like the
wallet's Razorpay rule).
"""
from __future__ import annotations

import os
from typing import Any

import httpx

GRAPH = "https://graph.facebook.com/v25.0"
BUTTON_TITLE_MAX = 20   # Cloud API hard limit; longer titles are rejected
BUTTONS_MAX = 3


def configured() -> bool:
    return bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_ID"))


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    """One send. Returns a result dict and never raises: the caller records
    the outcome; a messaging failure must never break a shopping flow."""
    if not configured():
        return {"mode": "outbox", "why": "WHATSAPP_TOKEN/WHATSAPP_PHONE_ID not set; "
                                         "message recorded, not delivered"}
    try:
        resp = httpx.post(
            f"{GRAPH}/{os.environ['WHATSAPP_PHONE_ID']}/messages",
            headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"},
            json=payload, timeout=20)
        body = resp.json()
        if resp.status_code >= 400:
            err = (body.get("error") or {})
            return {"mode": "failed", "why": err.get("message", resp.text[:200]),
                    "code": err.get("code")}
        wamid = ((body.get("messages") or [{}])[0]).get("id", "")
        return {"mode": "sent", "wamid": wamid}
    except Exception as exc:                              # network, DNS, timeout
        return {"mode": "failed", "why": f"{type(exc).__name__}: {exc}"}


def send_text(to_digits: str, body: str) -> dict[str, Any]:
    """Free-form text. Deliverable inside the 24h window the shopper opened
    by messaging the number first."""
    return _post({"messaging_product": "whatsapp", "to": to_digits,
                  "type": "text", "text": {"body": body[:4096]}})


def send_buttons(to_digits: str, body: str,
                 buttons: list[tuple[str, str]]) -> dict[str, Any]:
    """Text plus up to three tappable reply buttons.

    Each button is (id, title). The id comes back verbatim in the webhook when
    tapped - it references an offer record server-side and carries no secret,
    because the tap is authenticated by the webhook signature plus the sender's
    own WhatsApp number, not by anything hidden in the button.
    """
    if not buttons or len(buttons) > BUTTONS_MAX:
        raise ValueError(f"1..{BUTTONS_MAX} buttons required")
    for _bid, title in buttons:
        if len(title) > BUTTON_TITLE_MAX:
            raise ValueError(f"button title {title!r} exceeds {BUTTON_TITLE_MAX} chars")
    return _post({
        "messaging_product": "whatsapp", "to": to_digits, "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": bid, "title": title}}
                for bid, title in buttons]},
        }})
