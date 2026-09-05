r"""WhatsApp outreach: the agent goes TO the shopper (spec 16 addendum).

Until now every recovery flow waited for the shopper to come back to the site.
This module closes that gap for the two moments a real merchant would reach
out: a restock the shopper was refused on, and a cart they walked away from.

Policy in code, all of it:

- WhatsApp carries WORDS: cart lines, amounts, buttons, receipts. It never
  carries payment credentials, and money still moves only through
  common/wallet.py's five checks. This file signs nothing the widget's
  Approve tap would not have signed.
- The Approve button IS the human approval. The message states the exact
  basket and the exact amount; the tap comes back in a webhook signed by Meta
  (X-Hub-Signature-256 over the raw body, HMAC with the app secret) from the
  shopper's own verified number. That is a stronger identity claim than the
  widget's browser-local shopper key.
- Never charge more than the message said. The order is placed at tap time
  under a mandate capped at the quoted amount, so a dearer basket is refused
  by the shop's own OverCap gate; the agent then re-quotes with fresh buttons
  instead of paying. Cheaper (a coupon landed) is allowed and said out loud.
- Say once: one message per restock offer, one reminder per abandoned cart.
  A decline is recorded and never argued with.
- Everything is written down: every send lands in an outbox table (mode
  "sent", or "outbox" when credentials are absent - honestly undelivered)
  and on the buyer chain; every tap, refusal and payment is chain-logged.
- An unsigned or mis-signed webhook is refused with no side effects. A
  replayed webhook (Meta retries) is deduplicated on the message id. A tap
  from a number that is not the offer's shopper is refused and logged.

Nothing here runs unless a shopper gave the shop a phone number - the same
contact_key the demand ledger already keeps (a claim for reaching them, never
a credential for charging them).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from common import approval, chainlog, contact, errors, mandate, money, wallet, whatsapp

RESTOCK_OFFER_TTL = 48 * 3600     # a held restock is worth two days
CART_OFFER_TTL = 24 * 3600        # a cart reminder goes stale in one
LOGIN_CODE_TTL = 300              # an OTP lives five minutes
LOGIN_MAX_ATTEMPTS = 3            # then the code is burned, not brute-forced
LOGIN_RESEND_MAX = 3              # codes per contact per window
LOGIN_RESEND_WINDOW = 900
ABANDON_AFTER_SECONDS = int(os.environ.get("VELCROW_ABANDON_MINUTES", "30")) * 60
DEFAULT_CC = os.environ.get("WHATSAPP_DEFAULT_CC", "91")   # test tier is India-only
MAX_LINES_IN_MESSAGE = 6


# -- storage ------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "whatsapp.sqlite", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wa_offers (
          offer_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
          shop_id TEXT NOT NULL, shop_url TEXT NOT NULL,
          contact_key TEXT NOT NULL, wa_to TEXT NOT NULL,
          shopper_ref TEXT NOT NULL DEFAULT '',
          items TEXT NOT NULL, quoted_paise INTEGER NOT NULL,
          cart_id TEXT NOT NULL DEFAULT '', res_id TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          why TEXT NOT NULL DEFAULT '', txn_ref TEXT NOT NULL DEFAULT '',
          created_ts REAL NOT NULL, decided_ts REAL);
        CREATE TABLE IF NOT EXISTS wa_outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          wa_to TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
          mode TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          wamid TEXT NOT NULL DEFAULT '', created_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_contacts (
          contact_key TEXT PRIMARY KEY, wa_id TEXT NOT NULL,
          profile_name TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_processed (wamid TEXT PRIMARY KEY, ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_link_logins (
          link_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
          contact TEXT NOT NULL DEFAULT '', contact_key TEXT NOT NULL DEFAULT '',
          created_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_login_codes (
          contact_key TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
          expires_ts REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          sends TEXT NOT NULL DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS wa_chats (
          contact_key TEXT PRIMARY KEY, shop_key TEXT NOT NULL,
          cart_id TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]',
          updated_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS wa_goals (
          contact_key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
          goal TEXT NOT NULL, stage TEXT NOT NULL,
          options TEXT NOT NULL DEFAULT '[]', updated_ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cart_activity (
          cart_id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, shop_url TEXT NOT NULL,
          contact_key TEXT NOT NULL, shopper_ref TEXT NOT NULL DEFAULT '',
          last_seen REAL NOT NULL, reminded_ts REAL NOT NULL DEFAULT 0);
        """
    )
    # additive column for goal offers on an existing db
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wa_offers)")}
    if "run_id" not in cols:
        conn.execute("ALTER TABLE wa_offers ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
    return conn


def _mask(digits: str) -> str:
    return f"...{digits[-4:]}" if len(digits) >= 4 else "..."


def _reach(contact_key: str) -> str:
    """Digits WhatsApp can deliver to, or '' when this shopper is unreachable.

    A number that has messaged us is known exactly (wa_contacts). Otherwise a
    phone-type contact_key gets the default country code - documented test-tier
    assumption, and a wrong guess can reach nobody because the test number can
    only deliver to its verified list.
    """
    with _db() as c:
        row = c.execute("SELECT wa_id FROM wa_contacts WHERE contact_key = ?",
                        (contact_key,)).fetchone()
    if row:
        return row["wa_id"]
    if contact_key.startswith("phone:"):
        return DEFAULT_CC + contact_key.split(":", 1)[1]
    return ""


def _record_send(wa_to: str, kind: str, body: str, result: dict[str, Any]) -> None:
    with _db() as c:
        c.execute("INSERT INTO wa_outbox (wa_to, kind, body, mode, detail, wamid, created_ts) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (wa_to, kind, body, result["mode"], result.get("why", ""),
                   result.get("wamid", ""), time.time()))
    event = {"sent": "whatsapp_message_sent", "outbox": "whatsapp_message_outboxed",
             "failed": "whatsapp_message_failed"}[result["mode"]]
    why = {"sent": f"WhatsApp {kind} delivered to {_mask(wa_to)}",
           "outbox": f"WhatsApp {kind} for {_mask(wa_to)} written to the outbox only - "
                     "no credentials configured, honestly undelivered",
           "failed": f"WhatsApp {kind} to {_mask(wa_to)} failed: {result.get('why', '')}",
           }[result["mode"]]
    chainlog.append("buyer", event, why,
                    {"kind": kind, "to": _mask(wa_to), "mode": result["mode"],
                     "wamid": result.get("wamid", "")})


def _send_text(wa_to: str, kind: str, body: str) -> dict[str, Any]:
    result = whatsapp.send_text(wa_to, body)
    _record_send(wa_to, kind, body, result)
    return result


def _send_buttons(wa_to: str, kind: str, body: str,
                  buttons: list[tuple[str, str]]) -> dict[str, Any]:
    result = whatsapp.send_buttons(wa_to, body, buttons)
    _record_send(wa_to, kind, body, result)
    return result


def _approve_title(quoted_paise: int) -> str:
    title = f"Approve {money.rupees(quoted_paise)}"
    return title if len(title) <= whatsapp.BUTTON_TITLE_MAX else "Approve & pay"


# -- outgoing: restock ---------------------------------------------------------

def on_restock_offer(offer: dict[str, Any]) -> dict[str, Any]:
    """Called by /callback/restock AFTER the in-widget offer is held. Additive:
    the widget offer stands whether or not this message can be delivered."""
    contact_key = str(offer.get("contact_key") or "")
    wa_to = _reach(contact_key)
    if not wa_to:
        return {"messaged": False, "why": "no phone contact on the reservation"}
    with _db() as c:   # say once: per reservation, AND per person-per-item.
        # A restock fans out one callback per ledger row, and one shopper can
        # own several rows (refused three times for the same lemons). Four
        # pings about one restock is nagging, not service.
        # Only an OPEN offer blocks the person-per-item rule. An approved one
        # is finished business: they were told, they paid, the ledger row is
        # converted. Counting it forever meant one paid lemon rescue silenced
        # every lemon restock for that shopper for life (found live).
        dup = c.execute("SELECT offer_id FROM wa_offers WHERE kind = 'restock' AND ("
                        "(status IN ('pending', 'approved') AND res_id = ?) "
                        "OR (status = 'pending' AND contact_key = ? "
                        "AND json_extract(items, '$[0].item_id') = ?))",
                        (str(offer.get("res_id") or ""), contact_key,
                         str(offer.get("item_id") or ""))).fetchone()
    if dup:
        return {"messaged": False, "why": f"already messaged as {dup['offer_id']}"}

    qty = int(offer.get("qty", 1) or 1)
    unit = int(offer.get("unit_price_paise", 0))
    quoted = unit * qty
    if quoted <= 0:
        return {"messaged": False, "why": "no price on the offer; refusing to quote blind"}

    # Complete the basket, don't peddle the unit. If the refusal came from a
    # cart that still exists, the shopper's stated want was the WHOLE basket -
    # they asked for 6, got 5, and quoting the returned 1 at them ("pay ₹899")
    # while ignoring the other 5 reads as a different shop talking (found live
    # by the shopper it happened to). So: put the returned units INTO that
    # cart - their original ask, no money moved - and quote the full basket
    # with its coupons.
    completed = _complete_basket(offer) if offer.get("cart_id") else None
    if completed is not None:
        return completed

    items = [{"item_id": offer["item_id"], "variant": str(offer.get("variant") or ""),
              "qty": qty}]
    offer_id = "wa_" + uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                  "wa_to, shopper_ref, items, quoted_paise, res_id, title, created_ts) "
                  "VALUES (?, 'restock', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (offer_id, offer["shop_id"], offer.get("shop_url", ""), contact_key, wa_to,
                   str(offer.get("shopper_ref") or ""), json.dumps(items), quoted,
                   str(offer.get("res_id") or ""), offer.get("product_name", ""), time.time()))

    held = "We're holding yours." if offer.get("held", True) else \
           "It moves fast - nothing is held."
    body = (f"Good news from {offer['shop_id']}: {offer.get('product_name', offer['item_id'])}"
            f"{(' (' + offer['variant'] + ')') if offer.get('variant') else ''} is back in stock. "
            f"{held}\n\n"
            f"{qty} x {money.rupees(unit)} = {money.rupees(quoted)}\n\n"
            f"Tap Approve and it's paid and confirmed - you authorise exactly "
            f"{money.rupees(quoted)}, nothing else.")
    result = _send_buttons(wa_to, "restock_offer", body,
                           [(f"apr_{offer_id}", _approve_title(quoted)),
                            (f"dec_{offer_id}", "No thanks")])
    return {"messaged": result["mode"] == "sent", "offer_id": offer_id, "mode": result["mode"]}


def _complete_basket(offer: dict[str, Any]) -> dict[str, Any] | None:
    """Put the restocked units back into the basket they were refused from and
    quote the WHOLE basket. Returns None when the cart is gone or unusable -
    the caller falls back to the single-unit offer. Adding to a cart moves no
    money; it is the shopper's original ask, and paying still takes the tap."""
    contact_key = str(offer.get("contact_key") or "")
    wa_to = _reach(contact_key)
    cart_id = str(offer["cart_id"])
    qty = int(offer.get("qty", 1) or 1)
    with _db() as c:   # a second restock must not stuff the basket again
        # Pending only: an approved basket offer was paid and the cart emptied,
        # so a later refusal on the same cart id is a new basket, not a repeat.
        dup = c.execute("SELECT offer_id FROM wa_offers WHERE kind = 'cart' AND cart_id = ? "
                        "AND status = 'pending'", (cart_id,)).fetchone()
    if dup:
        return {"messaged": False, "why": f"basket already offered as {dup['offer_id']}"}
    try:
        with httpx.Client(base_url=offer.get("shop_url", ""), timeout=20) as http:
            view = http.get(f"/cart/{cart_id}")
            if view.status_code >= 400 or not (view.json().get("items")):
                return None                      # basket gone; offer the unit instead
            token = mandate.issue(1, 1, [offer["shop_id"]], ttl_seconds=120)
            add = http.post(f"/cart/{cart_id}/fulfil",
                            headers={"Authorization": f"Mandate {token}"},
                            json={"item_id": offer["item_id"],
                                  "variant": str(offer.get("variant") or ""),
                                  "qty": qty, "mode": "add",
                                  "contact_ref": wa_to,
                                  "shopper_ref": str(offer.get("shopper_ref") or "")})
            if add.status_code >= 400 or int(add.json().get("added", 0)) < 1:
                return None
            lines = http.get(f"/cart/{cart_id}").json()["items"]
            best = http.post(f"/cart/{cart_id}/coupons", json={}).json()["best"]
    except Exception:
        return None

    quoted = int(best["net_total_paise"])
    total_units = sum(int(l["qty"]) for l in lines)
    offer_id = "wa_" + uuid.uuid4().hex[:12]
    items = [{"item_id": l["item_id"], "variant": l.get("variant", ""),
              "qty": int(l["qty"])} for l in lines]
    with _db() as c:
        c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                  "wa_to, shopper_ref, items, quoted_paise, cart_id, res_id, title, created_ts) "
                  "VALUES (?, 'cart', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (offer_id, offer["shop_id"], offer.get("shop_url", ""), contact_key, wa_to,
                   str(offer.get("shopper_ref") or ""), json.dumps(items), quoted, cart_id,
                   str(offer.get("res_id") or ""),
                   f"basket completed: {offer.get('product_name', offer['item_id'])}",
                   time.time()))
    chainlog.append("buyer", "basket_completed_on_restock",
                    f"{offer.get('product_name', offer['item_id'])} is back at "
                    f"{offer['shop_id']}; the {qty} refused unit(s) were returned to cart "
                    f"{cart_id} ({total_units} item(s) now, {money.rupees(quoted)} with "
                    "coupons). No money moved - the basket is simply whole again",
                    {"offer_id": offer_id, "cart_id": cart_id, "shop_id": offer["shop_id"],
                     "quoted_paise": quoted})
    coupon_note = (f" (coupon {'+'.join(best['codes'])} applied)" if best.get("codes") else "")
    body = (f"{offer.get('product_name', offer['item_id'])} is back at {offer['shop_id']} - "
            f"and your basket is whole again: I've put the {qty} you were short back in. "
            f"{total_units} item(s), {money.rupees(quoted)} total{coupon_note}.\n\n"
            f"Tap Approve to pay exactly {money.rupees(quoted)} for the whole basket, "
            "or Not now - it stays in your cart either way.")
    result = _send_buttons(wa_to, "basket_completed", body,
                           [(f"apr_{offer_id}", _approve_title(quoted)),
                            (f"dec_{offer_id}", "Not now")])
    return {"messaged": result["mode"] == "sent", "offer_id": offer_id,
            "mode": result["mode"], "completed_basket": True}


# -- outgoing: abandoned carts -------------------------------------------------

def record_cart_activity(shop_id: str, shop_url: str, cart_id: str,
                         contact_key: str, shopper_ref: str) -> None:
    """Every widget turn stamps the cart. New activity clears any earlier
    reminder mark, so walking away twice can earn a second (single) reminder."""
    if not cart_id:
        return
    with _db() as c:
        c.execute("INSERT INTO cart_activity (cart_id, shop_id, shop_url, contact_key, "
                  "shopper_ref, last_seen, reminded_ts) VALUES (?, ?, ?, ?, ?, ?, 0) "
                  "ON CONFLICT(cart_id) DO UPDATE SET last_seen = excluded.last_seen, "
                  "reminded_ts = 0, "
                  "contact_key = CASE WHEN excluded.contact_key != '' "
                  "THEN excluded.contact_key ELSE contact_key END, "
                  "shopper_ref = CASE WHEN excluded.shopper_ref != '' "
                  "THEN excluded.shopper_ref ELSE shopper_ref END",
                  (cart_id, shop_id, shop_url, contact_key, shopper_ref, time.time()))


def sweep_abandoned(now: float | None = None) -> list[dict[str, Any]]:
    """The scheduler's job: find carts gone quiet with a reachable shopper,
    quote them honestly (coupons included) and remind once."""
    now = now or time.time()
    sent: list[dict[str, Any]] = []
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cart_activity WHERE reminded_ts = 0 AND last_seen < ?",
            (now - ABANDON_AFTER_SECONDS,)).fetchall()]
    for row in rows:
        wa_to = _reach(row["contact_key"])
        if not wa_to:
            continue          # may become reachable once they message us; retry next sweep
        try:
            with httpx.Client(base_url=row["shop_url"], timeout=15) as http:
                view = http.get(f"/cart/{row['cart_id']}").json()
                lines = view.get("items") or []
                if not lines:
                    with _db() as c:
                        c.execute("DELETE FROM cart_activity WHERE cart_id = ?",
                                  (row["cart_id"],))
                    continue
                best = http.post(f"/cart/{row['cart_id']}/coupons", json={}).json()["best"]
        except Exception as exc:
            chainlog.append("buyer", "whatsapp_sweep_skipped",
                            f"cart {row['cart_id']} could not be re-read from "
                            f"{row['shop_id']}: {exc}; no reminder sent",
                            {"cart_id": row["cart_id"], "shop_id": row["shop_id"]})
            continue

        quoted = int(best["net_total_paise"])
        items = [{"item_id": l["item_id"], "variant": l.get("variant", ""),
                  "qty": int(l["qty"])} for l in lines]
        offer_id = "wa_" + uuid.uuid4().hex[:12]
        with _db() as c:
            c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                      "wa_to, shopper_ref, items, quoted_paise, cart_id, title, created_ts) "
                      "VALUES (?, 'cart', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (offer_id, row["shop_id"], row["shop_url"], row["contact_key"], wa_to,
                       row["shopper_ref"], json.dumps(items), quoted, row["cart_id"],
                       f"{len(lines)} item(s) at {row['shop_id']}", time.time()))
            c.execute("UPDATE cart_activity SET reminded_ts = ? WHERE cart_id = ?",
                      (now, row["cart_id"]))

        shown = [f"- {l.get('name', l['item_id'])} x {l['qty']}  "
                 f"{money.rupees(int(l['unit_price_paise']) * int(l['qty']))}"
                 for l in lines[:MAX_LINES_IN_MESSAGE]]
        if len(lines) > MAX_LINES_IN_MESSAGE:
            shown.append(f"- and {len(lines) - MAX_LINES_IN_MESSAGE} more line(s)")
        coupon_note = (f"\nCoupon {'+'.join(best['codes'])} already applied: "
                       f"-{money.rupees(int(best['discount_paise']))}"
                       if best.get("codes") else "")
        body = (f"Still thinking it over? Your cart at {row['shop_id']} is waiting:\n\n"
                + "\n".join(shown) + coupon_note
                + f"\n\nTotal: {money.rupees(quoted)}. Tap Approve to pay exactly that "
                  "and it's done - or Not now, and I won't ask again.")
        result = _send_buttons(wa_to, "cart_reminder", body,
                               [(f"apr_{offer_id}", _approve_title(quoted)),
                                (f"dec_{offer_id}", "Not now")])
        chainlog.append("buyer", "whatsapp_reminder_composed",
                        f"cart {row['cart_id']} at {row['shop_id']} went quiet with "
                        f"{len(lines)} line(s) worth {money.rupees(quoted)}; reminded "
                        f"{_mask(wa_to)} once ({result['mode']})",
                        {"offer_id": offer_id, "cart_id": row["cart_id"],
                         "shop_id": row["shop_id"], "quoted_paise": quoted,
                         "mode": result["mode"]})
        sent.append({"offer_id": offer_id, "cart_id": row["cart_id"], "mode": result["mode"]})
    return sent


# -- incoming: the webhook -----------------------------------------------------

def verify_challenge(mode: str, token: str, challenge: str) -> str:
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise errors.MandateInvalid("webhook verify token mismatch", tier="webhook")


def _signature_ok(raw: bytes, header: str) -> bool:
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], digest)


def handle_webhook(raw: bytes, signature_header: str) -> dict[str, Any]:
    """Everything Meta POSTs lands here. Signature first, always; a body that
    cannot prove it came from Meta does nothing at all."""
    if not _signature_ok(raw, signature_header):
        chainlog.append("buyer", "whatsapp_webhook_refused",
                        "webhook signature missing or invalid; body ignored "
                        "(set WHATSAPP_APP_SECRET, and nobody but Meta can pass this gate)",
                        {"signed": signature_header[:16]})
        raise errors.MandateInvalid("webhook signature invalid", tier="webhook")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise errors.BadRequest("webhook body is not JSON")

    handled: list[dict[str, Any]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {ct.get("wa_id", ""): (ct.get("profile") or {}).get("name", "")
                     for ct in value.get("contacts", [])}
            for msg in value.get("messages", []):
                wamid = msg.get("id", "")
                with _db() as c:   # Meta retries deliveries; act once
                    try:
                        c.execute("INSERT INTO wa_processed (wamid, ts) VALUES (?, ?)",
                                  (wamid, time.time()))
                    except sqlite3.IntegrityError:
                        continue
                handled.append(_handle_message(msg, names.get(msg.get("from", ""), "")))
    return {"handled": handled}


def _bind(wa_id: str, profile_name: str) -> bool:
    """Remember which exact WhatsApp number a contact_key belongs to.
    Returns True the first time - that's the moment for one greeting."""
    try:
        key = contact.normalise(wa_id)
    except contact.InvalidContact:
        return False
    with _db() as c:
        try:
            c.execute("INSERT INTO wa_contacts (contact_key, wa_id, profile_name, first_seen) "
                      "VALUES (?, ?, ?, ?)", (key, wa_id, profile_name, time.time()))
        except sqlite3.IntegrityError:
            return False
    chainlog.append("buyer", "whatsapp_bound",
                    f"{_mask(wa_id)} messaged the agent's number; their contact key is now "
                    "reachable on WhatsApp exactly (no country-code guessing)",
                    {"contact_key": key, "to": _mask(wa_id)})
    return True


def _handle_message(msg: dict[str, Any], profile_name: str) -> dict[str, Any]:
    wa_id = str(msg.get("from", ""))
    if msg.get("type") == "interactive":
        reply = (msg.get("interactive") or {}).get("button_reply") or {}
        bid = str(reply.get("id", ""))
        if bid.startswith("apr_"):
            return approve_tap(bid[4:], wa_id)
        if bid.startswith("dec_"):
            return decline_tap(bid[4:], wa_id)
        return {"kind": "unknown_button", "id": bid}

    text = str(((msg.get("text") or {}).get("body")) or "").strip()

    # Any plain message binds the number; the first one earns the greeting.
    first = _bind(wa_id, profile_name)
    if first:
        _send_text(wa_id, "greeting",
                   "Namaste! This is the VelcrowAI agent. Tell me what you need - "
                   "\"3 chanderi dupattas\", \"1 kg lemons\" - and I'll shop for it. Say "
                   "freshkart, loomcraft or silkroute to switch shops, and ask to check out when "
                   "you're ready: I'll send an Approve button with the exact amount. "
                   "Nothing is ever paid without that tap, and I never ask for card or "
                   "UPI details.")
        if text.lower() in ("hi", "hello", "hey", "namaste", "hii"):
            return {"kind": "text", "bound": True}

    if not text:
        return {"kind": "text", "bound": first}

    # Everything else is the model's to handle - which shop it is about, what
    # to do, what to say. No keyword routing: the same tool loop the widget
    # runs, on a worker thread so Meta gets its 200 now and the reply follows
    # when the turn ends.
    _spawn(lambda: run_wa_turn(wa_id, text))
    return {"kind": "chat_turn_started"}


# -- conversational shopping over WhatsApp ------------------------------------
#
# The same brain, a third surface. A text like "3 chanderi dupattas" runs the
# EXACT model + tool loop the widget runs (runtime.run_turn): the model picks
# its own tools, the code gates every call, and if the model decides to quote
# (its start_checkout tool), the quote comes back here as Approve buttons.
# This module adds no intelligence and removes no gate - it is a mouth and an
# ear, and money still moves only through the wallet after the tap.

def _spawn(fn) -> None:
    import threading
    threading.Thread(target=fn, daemon=True).start()


def _shops() -> dict[str, dict[str, Any]]:
    from agent import app as agent_app       # lazy: agent.app imports this module
    return agent_app.INSTALLED_SHOPS


def active_cart(contact_key: str, shop_id: str) -> str:
    """The basket this person already has going at this shop, from either
    surface - one person, one basket, whichever mouth they use."""
    with _db() as c:
        chat = c.execute("SELECT shop_key, cart_id FROM wa_chats WHERE contact_key = ?",
                         (contact_key,)).fetchone()
        if chat and _shops().get(chat["shop_key"], {}).get("shop_id") == shop_id:
            return chat["cart_id"]
        act = c.execute("SELECT cart_id FROM cart_activity WHERE contact_key = ? "
                        "AND shop_id = ? ORDER BY last_seen DESC LIMIT 1",
                        (contact_key, shop_id)).fetchone()
    return act["cart_id"] if act else ""


_MENU_CACHE: dict[str, Any] = {"ts": 0.0, "menu": {}, "hay": {}, "products": {}}
_MENU_TTL = 600


def _refresh_catalog_cache() -> None:
    """One fetch serves both routing halves: `menu` (per-shop vocabulary
    sentences for the model's mode judgment) and `hay` (the full lowercase
    catalog text the deterministic shop chooser scores against)."""
    if time.time() - _MENU_CACHE["ts"] < _MENU_TTL and _MENU_CACHE["hay"]:
        return
    menu: dict[str, str] = {}
    hay: dict[str, str] = {}
    products: dict[str, list[dict[str, Any]]] = {}
    for key, ins in _shops().items():
        words: list[str] = []
        # A shop's own name is part of its vocabulary, so "UrbanNest" as a
        # reply to "which one?" routes there.
        pieces: list[str] = [ins["name"], ins["shop_id"]]
        products[key] = []
        try:
            with httpx.Client(base_url=ins["url"], timeout=8) as http:
                for p in http.get("/catalog").json():
                    products[key].append(p)
                    pieces.append(f"{p.get('name', '')} {p.get('category', '')} "
                                  f"{' '.join(p.get('tags', []))}")
                    for w in [p.get("category", "")] + list(p.get("tags", [])):
                        if w and w not in words:
                            words.append(w)
        except Exception:
            pass
        vocab = ", ".join(words[:14])
        menu[key] = f"{ins['name']} ({ins['category']}" + (f": sells {vocab}" if vocab else "") + ")"
        hay[key] = " ".join(pieces).lower()
    _MENU_CACHE.update(menu=menu, hay=hay, products=products)
    if any(hay.values()):
        _MENU_CACHE["ts"] = time.time()


def _shop_menu() -> dict[str, str]:
    """What each shop actually SELLS, for the router's mode judgment - names
    and categories alone once routed 'are there any cushions' to the grocer,
    because nothing told the model who sells cushions."""
    _refresh_catalog_cache()
    return dict(_MENU_CACHE["menu"])


def _catalog_hay() -> dict[str, str]:
    """Each shop's full catalog text, lowercased, for the deterministic shop
    chooser."""
    _refresh_catalog_cache()
    return dict(_MENU_CACHE["hay"])


def _route_message(text: str, current_key: str | None) -> dict[str, str]:
    """Instruction within a shop, or a goal to satisfy across every shop?
    The orchestrator's call - one place owns routing between agents."""
    from agent import orchestrator
    return orchestrator.route(text, current_key, _shop_menu(), _route_shop)


def _route_shop(text: str, current_key: str | None) -> str:
    """Which shop the message is about - pure vocabulary lookup against the
    live catalogs. Deliberately NOT a model: 'Dupattas' kept staying at the
    grocer when a model did this, because matching words to catalogs is
    lexical work, not judgment. A shop that names the goods wins; ties keep
    the current shop; a message naming no goods stays where it is."""
    return _route_shop_candidates(text, current_key)[0]


def _query_words(text: str) -> set[str]:
    raw_words = {w for w in "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
                 if len(w) >= 3}
    return raw_words | {w[:-1] for w in raw_words if w.endswith("s") and len(w) > 3}


def _route_shop_candidates(text: str, current_key: str | None) -> list[str]:
    """Every shop that names the goods equally well. One entry when the
    message stays put or the current shop is among the winners; several when
    the shopper is jumping and more than one shop sells it - then the honest
    answer is to show them all, not to pick the first (found live: "cushions"
    from the grocer silently went to UrbanNest while MittiCraft sold them too)."""
    words = _query_words(text)
    scores: dict[str, int] = {}
    for key, hay in _catalog_hay().items():
        scores[key] = sum(1 for w in words if w in hay)
    best = max(scores.values() or [0])
    if best <= 0:
        return [current_key or "grocery"]
    winners = [k for k, v in scores.items() if v == best]
    if current_key in winners:
        return [current_key]
    return winners


def _chat_state(contact_key: str, shop_key: str) -> dict[str, Any]:
    with _db() as c:
        row = c.execute("SELECT * FROM wa_chats WHERE contact_key = ?",
                        (contact_key,)).fetchone()
    installed = _shops()[shop_key]
    switched = bool(row) and row["shop_key"] != shop_key
    cart_id = "" if switched else (row["cart_id"] if row else "")
    if not cart_id:
        # One person, one basket: if the widget or storefront already knows
        # this shopper's cart at this shop, WhatsApp joins THAT basket rather
        # than opening a rival one (found live: "it said it added lemons but I
        # can't see them in my cart" - they were in a second basket).
        cart_id = active_cart(contact_key, installed["shop_id"])
    try:
        with httpx.Client(base_url=installed["url"], timeout=10) as http:
            if not cart_id or http.get(f"/cart/{cart_id}").status_code >= 400:
                cart_id = http.post("/cart", json={}).json()["cart_id"]
    except Exception:
        cart_id = cart_id or ""
    history = [] if switched else (json.loads(row["history"]) if row else [])
    with _db() as c:
        c.execute("INSERT INTO wa_chats (contact_key, shop_key, cart_id, history, updated_ts) "
                  "VALUES (?, ?, ?, ?, ?) ON CONFLICT(contact_key) DO UPDATE SET "
                  "shop_key = excluded.shop_key, cart_id = excluded.cart_id, "
                  "history = excluded.history, updated_ts = excluded.updated_ts",
                  (contact_key, shop_key, cart_id, json.dumps(history), time.time()))
    return {"shop_key": shop_key, "shop_id": installed["shop_id"],
            "shop_url": installed["url"], "cart_id": cart_id, "history": history,
            "switched": switched}


def _remember_turn(contact_key: str, user_text: str, reply: str) -> None:
    with _db() as c:
        row = c.execute("SELECT history FROM wa_chats WHERE contact_key = ?",
                        (contact_key,)).fetchone()
        history = json.loads(row["history"]) if row else []
        history = (history + [{"role": "user", "content": user_text},
                              {"role": "assistant", "content": reply}])[-12:]
        c.execute("UPDATE wa_chats SET history = ?, updated_ts = ? WHERE contact_key = ?",
                  (json.dumps(history), time.time(), contact_key))


def _agent_turn(chat: dict[str, Any], text: str, contact_key: str) -> list[dict[str, Any]]:
    """One real widget turn, run to completion on this thread. Returns the
    run's events. Split out so tests can stand in a fake brain."""
    import asyncio

    from agent import app as agent_app, runtime
    installed = _shops()[chat["shop_key"]]
    token = mandate.issue(agent_app.DEFAULT_MAX_TOTAL_PAISE,
                          agent_app.DEFAULT_MAX_PER_TXN_PAISE, [installed["shop_id"]])
    claims = mandate.verify(token)
    run = runtime.new_run(installed["shop_id"])
    chainlog.append("buyer", "agent_turn_started",
                    f"shopper asked the agent over WhatsApp at {installed['name']}: {text!r}",
                    {"run_id": run.run_id, "shop_id": installed["shop_id"],
                     "cart_id": chat["cart_id"], "surface": "whatsapp"})
    asyncio.run(runtime.run_turn(run, installed, chat["cart_id"], text,
                                 chat["history"], claims, mandate_token=token,
                                 shopper_ref="", contact_key=contact_key,
                                 surface="whatsapp", notes=chat.get("notes"),
                                 also_sold_at=chat.get("also_sold_at")))
    return run.events


def run_wa_turn(wa_id: str, text: str) -> dict[str, Any]:
    """Text in, agent's reply out - and if the MODEL chose to quote, the quote
    comes back as Approve buttons bound to that exact order and amount."""
    try:
        key = contact.normalise(wa_id)
    except contact.InvalidContact:
        return {"kind": "chat", "why": "unusable sender"}

    # A goal conversation in progress claims the message first: a bare "2" is
    # picking option 2, and the answer to "what's your budget" is a budget.
    pending = _goal_state(key)
    if pending and pending["stage"] == "options" and text.strip().isdigit():
        return _goal_choose(wa_id, key, pending, int(text.strip()))
    if pending and pending["stage"] == "clarify":
        return _goal_start(wa_id, key, f"{pending['goal']} {text}")

    with _db() as c:
        row = c.execute("SELECT shop_key FROM wa_chats WHERE contact_key = ?",
                        (key,)).fetchone()
    from agent import orchestrator
    decide = _route_message(text, row["shop_key"] if row else None)
    if decide["mode"] == "goal":
        orchestrator.handoff("whatsapp", "buyer-agent",
                             f"routed {text[:60]!r} as a cross-shop goal")
        return _goal_start(wa_id, key, text)

    # Facts for the model, never a decision made for it: when the goods the
    # shopper named are sold by more than one shop, the assistant is told so
    # and can search the network and offer every option in its own words.
    candidates = _route_shop_candidates(text, row["shop_key"] if row else None)
    orchestrator.handoff("whatsapp", "shopping-assistant",
                         f"routed {text[:60]!r} to {decide['shop']}",
                         shop_key=decide["shop"], also_sold_at=candidates[1:])
    try:
        chat = _chat_state(key, decide["shop"])
        if not chat["cart_id"]:
            _send_text(wa_id, "chat_reply",
                       "The shop isn't answering right now, so I couldn't start a basket. "
                       "Try again in a minute.")
            return {"kind": "chat", "why": "shop unreachable"}
        record_cart_activity(chat["shop_id"], chat["shop_url"], chat["cart_id"], key, "")
        if len(candidates) > 1:
            others = [_shops()[k]["name"] for k in candidates if k != decide["shop"]]
            chat["also_sold_at"] = others
            chat["notes"] = [f"What the shopper just asked for is ALSO sold by {', '.join(others)}. "
                             "Call search_network INSTEAD of search_catalog so they can see every "
                             "shop's options and prices, then let them choose."]
        events = _agent_turn(chat, text, key)
        # The model may have decided to move the conversation to another shop.
        moved = next((e for e in events if e.get("kind") == "switch_shop"), None)
        if moved and moved.get("shop_key") in _shops():
            _chat_state(key, moved["shop_key"])
            orchestrator.handoff("whatsapp", "shopping-assistant",
                                 f"the assistant moved the chat to {moved['shop_key']}",
                                 shop_key=moved["shop_key"])
    except Exception as exc:
        chainlog.append("buyer", "whatsapp_turn_failed",
                        f"WhatsApp turn for {_mask(wa_id)} failed before any reply: {exc}",
                        {"error": str(exc)[:200]})
        _send_text(wa_id, "chat_reply",
                   "Something went wrong on my side - nothing was bought or charged. "
                   "Say it again and I'll retry.")
        return {"kind": "chat", "why": "turn failed"}

    reply = next((e["text"] for e in reversed(events)
                  if e.get("kind") == "message" and e.get("text")), "")
    quote = next((e for e in events if e.get("kind") == "approval_required"), None)

    # Formatting is code work: markdown bold is "**x**", WhatsApp bold is
    # "*x*", and a model told "no asterisks" still slips some in.
    reply = reply.replace("**", "*")
    if quote is None:
        _send_text(wa_id, "chat_reply", reply or "Done. Anything else?")
        _remember_turn(key, text, reply)
        return {"kind": "chat", "replied": True}

    # The model quoted and stopped, exactly as in the widget. The tap on this
    # button is the human approval; the amount below is the ONLY amount the
    # tap can authorise.
    quoted = int(quote["charge_amount_paise"])
    offer_id = "wa_" + uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("UPDATE wa_offers SET status = 'superseded', why = 'newer quote' "
                  "WHERE kind = 'quote' AND cart_id = ? AND status = 'pending'",
                  (chat["cart_id"],))
        c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                  "wa_to, shopper_ref, items, quoted_paise, cart_id, title, txn_ref, "
                  "created_ts) VALUES (?, 'quote', ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
                  (offer_id, chat["shop_id"], chat["shop_url"], key, wa_id,
                   json.dumps([{"item_id": li["item_id"], "variant": li.get("variant", ""),
                                "qty": int(li["qty"])} for li in quote.get("line_items", [])]),
                   quoted, chat["cart_id"], "agent quote", quote["txn_ref"], time.time()))
    chainlog.append("buyer", "whatsapp_quote_offered",
                    f"the agent quoted {quote['txn_ref']} at {money.rupees(quoted)} over "
                    f"WhatsApp and stopped for the human's tap - the same approval gate as "
                    "the widget, in a different pocket",
                    {"offer_id": offer_id, "txn_ref": quote["txn_ref"],
                     "amount_paise": quoted, "shop_id": chat["shop_id"]})
    lines = "\n".join(f"- {li.get('name', li['item_id'])} x {li['qty']}  "
                      f"{li.get('line_total_display', '')}"
                      for li in quote.get("line_items", [])[:MAX_LINES_IN_MESSAGE])
    body = ((reply + "\n\n") if reply else "") + \
        (f"{lines}\n\nTotal: {money.rupees(quoted)}. Tap Approve to pay exactly that - "
         "the price is locked for 5 minutes.")
    _send_buttons(wa_id, "quote", body,
                  [(f"apr_{offer_id}", _approve_title(quoted)),
                   (f"dec_{offer_id}", "Not now")])
    _remember_turn(key, text, reply or f"(quoted {money.rupees(quoted)})")
    return {"kind": "chat", "quoted": True, "offer_id": offer_id}


def _approve_quote(offer: dict[str, Any]) -> dict[str, Any]:
    """Pay the order the model already placed - the WhatsApp twin of /pay.
    The approval is signed over the QUOTED amount; a shop demanding anything
    else is refused by the wallet's own checks."""
    offer_id, quoted = offer["offer_id"], int(offer["quoted_paise"])
    token = mandate.issue(quoted, quoted, [offer["shop_id"]], ttl_seconds=600)
    claims = mandate.verify(token)
    try:
        with httpx.Client(base_url=offer["shop_url"], timeout=30) as http:
            order = http.get(f"/order/{offer['txn_ref']}")
            if order.status_code >= 400 or order.json().get("status") != "pending":
                _finish(offer_id, "expired", why="quote no longer pending")
                _send_text(offer["wa_to"], "expired",
                           "That quote's 5-minute price lock has passed, so nothing was "
                           "charged. Ask me to check out again and I'll re-quote it.")
                return {"kind": "approve", "offer_id": offer_id, "why": "quote expired"}
            appr = approval.issue(offer["shop_id"], offer["txn_ref"],
                                  order.json()["line_items"], quoted, claims["jti"])
            chainlog.append("buyer", "approval_signed",
                            f"WhatsApp tap approved quote {offer['txn_ref']} from "
                            f"{offer['shop_id']} at {quoted} paise; cart-bound approval "
                            "signed (5-minute window, single-use nonce)",
                            {"offer_id": offer_id, "txn_ref": offer["txn_ref"],
                             "shop_id": offer["shop_id"], "amount_paise": quoted,
                             "jti": claims["jti"]})
            result = wallet.pay(token, appr, offer["shop_id"], quoted, offer["txn_ref"],
                                shop_url=offer["shop_url"])
            confirm = http.post("/confirm-payment",
                                headers={"Idempotency-Key": f"confirm-{offer['txn_ref']}"},
                                json={"txn_ref": offer["txn_ref"],
                                      "razorpay_order_id": result["razorpay_order_id"],
                                      "payment_ref": result["payment_ref"]})
            confirmed = confirm.status_code < 400 and confirm.json().get("status") == "paid"
    except errors.VelcrowError as exc:
        _finish(offer_id, "blocked", why=f"{exc.code}: {exc.why}")
        chainlog.append("buyer", "whatsapp_checkout_blocked",
                        f"quote {offer_id} refused at the wallet ({exc.code}): {exc.why}; "
                        "nothing charged", {"offer_id": offer_id, "code": exc.code})
        _send_text(offer["wa_to"], "blocked",
                   f"I did NOT pay: {exc.why}. Nothing was charged.")
        return {"kind": "approve", "offer_id": offer_id, "blocked": exc.code}
    except Exception as exc:
        _finish(offer_id, "blocked", why=str(exc))
        _send_text(offer["wa_to"], "blocked",
                   "Something went wrong before any money moved, so nothing was charged.")
        return {"kind": "approve", "offer_id": offer_id, "blocked": "error"}

    _finish(offer_id, "approved", txn_ref=offer["txn_ref"])
    if offer["cart_id"]:
        with _db() as c:
            c.execute("DELETE FROM cart_activity WHERE cart_id = ?", (offer["cart_id"],))
    chainlog.append("buyer", "whatsapp_checkout_paid",
                    f"quote {offer_id} paid: {money.rupees(quoted)} to {offer['shop_id']} on "
                    f"{offer['txn_ref']}, confirmed={confirmed}",
                    {"offer_id": offer_id, "txn_ref": offer["txn_ref"],
                     "shop_id": offer["shop_id"], "amount_paise": quoted,
                     "confirmed": confirmed})
    _send_text(offer["wa_to"], "receipt",
               f"Paid {money.rupees(quoted)} to {offer['shop_id']} (ref {offer['txn_ref']}). "
               "They charged exactly what you approved - the wallet checked.")
    return {"kind": "approve", "offer_id": offer_id, "paid": True,
            "txn_ref": offer["txn_ref"], "amount_paise": quoted}


# -- cross-shop goal shopping over WhatsApp (spec 8, third surface) -----------
#
# "find me a cotton kurti under 1500" is not an instruction to a shop - it is
# a WANT, and satisfying it is the consumer buyer agent's job: mandate minted
# FROM the stated budget (the wallet's cap is the shopper's own words), every
# shop searched, options ranked by price, fit, and the trust each shop has
# EARNED, rule-breakers refused server-side. This section only carries that
# flow into the chat; the deciding happens in the same /buyer machinery the
# buyer app uses.

GOAL_TTL = 3600   # a goal conversation goes stale in an hour


def _self_url() -> str:
    return os.environ.get("VELCROW_SELF_URL", "http://127.0.0.1:8003")


def _buyer_call(method: str, path: str, body: dict[str, Any] | None = None
                ) -> tuple[int, dict[str, Any]]:
    """The buyer agent's own HTTP surface - the same one the buyer app calls,
    so WhatsApp cannot grow a private variant of the flow."""
    with httpx.Client(base_url=_self_url(), timeout=120) as http:
        r = http.request(method, path, json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}


def _goal_state(contact_key: str) -> dict[str, Any] | None:
    with _db() as c:
        row = c.execute("SELECT * FROM wa_goals WHERE contact_key = ?",
                        (contact_key,)).fetchone()
        if row and time.time() > row["updated_ts"] + GOAL_TTL:
            c.execute("DELETE FROM wa_goals WHERE contact_key = ?", (contact_key,))
            return None
    if row is None:
        return None
    out = dict(row)
    out["options"] = json.loads(out["options"])
    return out


def _set_goal(contact_key: str, run_id: str, goal: str, stage: str,
              options: list[dict[str, Any]]) -> None:
    with _db() as c:
        c.execute("INSERT INTO wa_goals (contact_key, run_id, goal, stage, options, "
                  "updated_ts) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(contact_key) DO UPDATE "
                  "SET run_id = excluded.run_id, goal = excluded.goal, "
                  "stage = excluded.stage, options = excluded.options, "
                  "updated_ts = excluded.updated_ts",
                  (contact_key, run_id, goal, stage, json.dumps(options), time.time()))


def _clear_goal(contact_key: str) -> None:
    with _db() as c:
        c.execute("DELETE FROM wa_goals WHERE contact_key = ?", (contact_key,))


def _said(state: dict[str, Any], kind: str) -> str:
    return next((m["text"] for m in reversed(state.get("messages", []))
                 if m.get("kind") == kind), "")


def _goal_start(wa_id: str, contact_key: str, goal: str) -> dict[str, Any]:
    code, state = _buyer_call("POST", "/buyer/run", {"goal": goal})
    if code >= 400:
        _clear_goal(contact_key)
        _send_text(wa_id, "chat_reply",
                   state.get("why", "I couldn't start on that goal - try wording it "
                                    "differently."))
        return {"kind": "goal", "why": "refused"}

    status = state.get("status", "")
    if status == "needs_clarification":
        _set_goal(contact_key, state["run_id"], goal, "clarify", [])
        _send_text(wa_id, "goal_ask",
                   _said(state, "ask") or "Tell me roughly what you want to spend.")
        return {"kind": "goal", "stage": "clarify", "run_id": state["run_id"]}

    options = state.get("options", [])
    if status in ("no_match", "all_rule_breaking") or not options:
        _clear_goal(contact_key)
        _send_text(wa_id, "goal_result",
                   _said(state, "note") or "Nothing matched at any shop.")
        return {"kind": "goal", "stage": status or "no_match"}

    shown = options[: 4]
    rows = []
    for i, o in enumerate(shown, 1):
        if o.get("selectable"):
            rows.append(f"{i}. {o['name']} - {o['price_display']} at {o['shop_name']}")
        else:
            why = "; ".join(o.get("breaks_rules", [])) or "breaks a rule you set"
            rows.append(f"{i}. {o['name']} at {o['shop_name']} - can't buy: {why}")
    budget = (state.get("rules") or {}).get("budget_display", "")
    _set_goal(contact_key, state["run_id"], goal, "options", shown)
    _send_text(wa_id, "goal_options",
               (f"I shopped every store for {goal!r}"
                + (f" (your cap {budget} is enforced by the wallet, not by promises)"
                   if budget else "") + ":\n\n" + "\n".join(rows)
                + "\n\nReply with a number and I'll get you a firm quote to approve. "
                  "Ranked on price, fit, stock, and how much each shop has earned my "
                  "trust."))
    chainlog.append("buyer", "whatsapp_goal_ranked",
                    f"goal {goal!r} over WhatsApp: {len(shown)} option(s) shown from "
                    f"run {state['run_id']}",
                    {"run_id": state["run_id"], "options": len(shown)})
    return {"kind": "goal", "stage": "options", "run_id": state["run_id"]}


def _goal_choose(wa_id: str, contact_key: str, pending: dict[str, Any],
                 number: int) -> dict[str, Any]:
    if not (1 <= number <= len(pending["options"])):
        _send_text(wa_id, "goal_ask",
                   f"Pick a number between 1 and {len(pending['options'])}.")
        return {"kind": "goal", "why": "bad option number"}
    option = pending["options"][number - 1]
    code, state = _buyer_call("POST", f"/buyer/run/{pending['run_id']}/choose",
                              {"option_id": option["option_id"]})
    if code >= 400 or state.get("status") != "awaiting_approval":
        why = (state.get("why") or _said(state, "blocked")
               or "that option can't be quoted right now")
        _send_text(wa_id, "goal_result", f"No quote: {why}. Nothing was charged.")
        return {"kind": "goal", "why": "choose refused"}

    quote = state["quote"]
    quoted = int(quote["charge_amount"])
    offer_id = "wa_" + uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("UPDATE wa_offers SET status = 'superseded', why = 'newer goal quote' "
                  "WHERE kind = 'goal' AND contact_key = ? AND status = 'pending'",
                  (contact_key,))
        c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                  "wa_to, items, quoted_paise, title, txn_ref, run_id, created_ts) "
                  "VALUES (?, 'goal', ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
                  (offer_id, quote.get("shop_id", ""), quote.get("shop_url", ""),
                   contact_key, wa_id, quoted, option["name"], quote["txn_ref"],
                   pending["run_id"], time.time()))
    _set_goal(contact_key, pending["run_id"], pending["goal"], "quoted", pending["options"])
    _send_buttons(wa_id, "goal_quote",
                  f"{option['name']} from {quote.get('shop_name', quote.get('shop_id'))} "
                  f"comes to {quote['charge_display']}. Tap Approve and I pay through the "
                  "wallet's five checks - it cannot pay a paisa more than this.",
                  [(f"apr_{offer_id}", _approve_title(quoted)),
                   (f"dec_{offer_id}", "Not now")])
    chainlog.append("buyer", "whatsapp_goal_quoted",
                    f"option {number} ({option['name']}) chosen over WhatsApp; quote "
                    f"{quote['txn_ref']} at {quote['charge_display']} awaits the tap",
                    {"run_id": pending["run_id"], "txn_ref": quote["txn_ref"],
                     "offer_id": offer_id})
    return {"kind": "goal", "stage": "quoted", "offer_id": offer_id}


def _approve_goal(offer: dict[str, Any]) -> dict[str, Any]:
    """The tap on a cross-shop quote: the buyer agent's own /approve does the
    approval + wallet + trust-scoring, exactly as in the buyer app."""
    offer_id = offer["offer_id"]
    code, state = _buyer_call("POST", f"/buyer/run/{offer['run_id']}/approve", {})
    status = state.get("status", "")
    if code >= 400 or status != "paid":
        why = (_said(state, "blocked") or state.get("why")
               or "the shop refused at the wallet")
        _finish(offer_id, "blocked", why=why[:200])
        chainlog.append("buyer", "whatsapp_checkout_blocked",
                        f"goal offer {offer_id} refused: {why}; nothing charged",
                        {"offer_id": offer_id, "run_id": offer["run_id"]})
        _send_text(offer["wa_to"], "blocked", f"I did NOT pay: {why}")
        return {"kind": "approve", "offer_id": offer_id, "blocked": True}
    receipt = state.get("receipt") or {}
    _finish(offer_id, "approved", txn_ref=str(receipt.get("txn_ref", "")))
    _clear_goal(offer["contact_key"])
    chainlog.append("buyer", "whatsapp_checkout_paid",
                    f"goal offer {offer_id} paid via the buyer agent's own approve path; "
                    "trust score updated for the shop that charged exactly what was quoted",
                    {"offer_id": offer_id, "run_id": offer["run_id"]})
    _send_text(offer["wa_to"], "receipt",
               _said(state, "receipt") or "Paid. They charged exactly what you approved.")
    return {"kind": "approve", "offer_id": offer_id, "paid": True}


# -- the tap: decline ----------------------------------------------------------

def _claim(offer_id: str, new_status: str) -> dict[str, Any] | None:
    """Atomically take a pending offer exactly once (double taps do nothing)."""
    with _db() as c:
        row = c.execute("SELECT * FROM wa_offers WHERE offer_id = ?", (offer_id,)).fetchone()
        if row is None or row["status"] != "pending":
            return None
        c.execute("UPDATE wa_offers SET status = ?, decided_ts = ? "
                  "WHERE offer_id = ? AND status = 'pending'",
                  (new_status, time.time(), offer_id))
        if c.execute("SELECT changes()").fetchone()[0] != 1:
            return None
    return dict(row)


def _finish(offer_id: str, status: str, why: str = "", txn_ref: str = "") -> None:
    with _db() as c:
        c.execute("UPDATE wa_offers SET status = ?, why = ?, txn_ref = ? WHERE offer_id = ?",
                  (status, why, txn_ref, offer_id))


def _sender_is_shopper(offer: dict[str, Any], wa_id: str) -> bool:
    try:
        return contact.normalise(wa_id) == offer["contact_key"]
    except contact.InvalidContact:
        return False


def decline_tap(offer_id: str, wa_id: str) -> dict[str, Any]:
    offer = _claim(offer_id, "declined")
    if offer is None:
        return {"kind": "decline", "offer_id": offer_id, "why": "not pending"}
    if not _sender_is_shopper(offer, wa_id):
        _finish(offer_id, "pending")   # not their offer; put it back
        chainlog.append("buyer", "whatsapp_tap_rejected",
                        f"decline on {offer_id} came from {_mask(wa_id)}, not the offer's "
                        "shopper; ignored", {"offer_id": offer_id})
        return {"kind": "decline", "offer_id": offer_id, "why": "sender mismatch"}
    chainlog.append("buyer", "whatsapp_offer_declined",
                    f"shopper said no to {offer['kind']} offer {offer_id} at "
                    f"{offer['shop_id']}; nothing added, nothing charged, not asked again",
                    {"offer_id": offer_id, "shop_id": offer["shop_id"], "kind": offer["kind"]})
    _send_text(offer["wa_to"], "decline_ack", "Done - I won't ask about that again.")
    return {"kind": "decline", "offer_id": offer_id, "declined": True}


# -- the tap: approve = the one door to money ---------------------------------

def approve_tap(offer_id: str, wa_id: str) -> dict[str, Any]:
    """The human's tap, WhatsApp dialect. Same shape as /pay in agent/app.py:
    place the order, sign the cart-bound approval over EXACTLY what the shop
    will charge, run the wallet's five checks, confirm, receipt."""
    offer = _claim(offer_id, "taken")
    if offer is None:
        return {"kind": "approve", "offer_id": offer_id, "why": "not pending"}
    if not _sender_is_shopper(offer, wa_id):
        _finish(offer_id, "pending")
        chainlog.append("buyer", "whatsapp_tap_rejected",
                        f"approve on {offer_id} came from {_mask(wa_id)}, not the offer's "
                        "shopper; refused with no side effects", {"offer_id": offer_id})
        return {"kind": "approve", "offer_id": offer_id, "why": "sender mismatch"}

    if offer["kind"] == "quote":
        return _approve_quote(offer)
    if offer["kind"] == "goal":
        return _approve_goal(offer)

    ttl = RESTOCK_OFFER_TTL if offer["kind"] == "restock" else CART_OFFER_TTL
    if time.time() > offer["created_ts"] + ttl:
        _finish(offer_id, "expired", why="tapped after expiry")
        _send_text(offer["wa_to"], "expired",
                   "That offer has expired, so nothing was charged. Ask me on the shop "
                   "page and I'll quote it fresh.")
        return {"kind": "approve", "offer_id": offer_id, "why": "expired"}

    quoted = int(offer["quoted_paise"])
    items = json.loads(offer["items"])
    chainlog.append("buyer", "whatsapp_tap_received",
                    f"shopper {_mask(wa_id)} tapped Approve on {offer['kind']} offer "
                    f"{offer_id}: {money.rupees(quoted)} at {offer['shop_id']}. The tap "
                    "authorises exactly that amount - now the same gates as any checkout",
                    {"offer_id": offer_id, "shop_id": offer["shop_id"],
                     "quoted_paise": quoted, "kind": offer["kind"]})

    # A mandate minted FROM the tap, capped at the amount the human saw.
    token = mandate.issue(quoted, quoted, [offer["shop_id"]], ttl_seconds=600)
    claims = mandate.verify(token)
    auth = {"Authorization": f"Mandate {token}"}

    try:
        with httpx.Client(base_url=offer["shop_url"], timeout=30) as http:
            cart_id = offer["cart_id"]
            if offer["kind"] == "restock":
                cart_id = http.post("/cart", json={}).json()["cart_id"]
                for it in items:
                    r = http.post(f"/cart/{cart_id}/fulfil", headers=auth,
                                  json={"item_id": it["item_id"], "variant": it["variant"],
                                        "qty": it["qty"], "mode": "add",
                                        "contact_ref": offer["wa_to"],
                                        "shopper_ref": offer["shopper_ref"]})
                    if r.status_code >= 400:
                        raise _shop_refusal(r)
            placed = http.post("/order",
                               headers={**auth, "Idempotency-Key": f"wa-{offer_id}"},
                               json={"cart_id": cart_id, "assisted": True,
                                     "source": "whatsapp",
                                     "shopper_ref": offer["shopper_ref"],
                                     "contact": offer["wa_to"]})
            if placed.status_code >= 400:
                raise _shop_refusal(placed)
            quote = placed.json()

            charge = int(quote["charge_amount"])
            if charge > quoted:
                # The shop's OverCap gate would refuse this anyway (the mandate is
                # capped at the quoted amount); getting here means pricing moved
                # between message and tap some other way. Same answer: requote.
                raise errors.PriceChanged(
                    "the basket now costs more than the message said",
                    old_amount=quoted, new_amount=charge)

            appr = approval.issue(offer["shop_id"], quote["txn_ref"],
                                  quote["line_items"], charge, claims["jti"])
            chainlog.append("buyer", "approval_signed",
                            f"WhatsApp tap approved basket {quote['txn_ref']} from "
                            f"{offer['shop_id']} at {charge} paise (message quoted {quoted}); "
                            "cart-bound approval signed (5-minute window, single-use nonce)",
                            {"offer_id": offer_id, "txn_ref": quote["txn_ref"],
                             "shop_id": offer["shop_id"], "amount_paise": charge,
                             "jti": claims["jti"]})
            result = wallet.pay(token, appr, offer["shop_id"], charge, quote["txn_ref"],
                                shop_url=offer["shop_url"])
            confirm = http.post("/confirm-payment",
                                headers={"Idempotency-Key": f"confirm-{quote['txn_ref']}"},
                                json={"txn_ref": quote["txn_ref"],
                                      "razorpay_order_id": result["razorpay_order_id"],
                                      "payment_ref": result["payment_ref"]})
            confirmed = confirm.status_code < 400 and confirm.json().get("status") == "paid"
    except errors.PriceChanged as exc:
        return _requote(offer, exc)
    except errors.VelcrowError as exc:
        _finish(offer_id, "blocked", why=f"{exc.code}: {exc.why}")
        chainlog.append("buyer", "whatsapp_checkout_blocked",
                        f"offer {offer_id} could not be completed: {exc.why}; nothing charged",
                        {"offer_id": offer_id, "code": exc.code, "shop_id": offer["shop_id"]})
        _send_text(offer["wa_to"], "blocked",
                   f"I couldn't complete that: {exc.why}. Nothing was charged.")
        return {"kind": "approve", "offer_id": offer_id, "blocked": exc.code}
    except Exception as exc:
        _finish(offer_id, "blocked", why=str(exc))
        chainlog.append("buyer", "whatsapp_checkout_blocked",
                        f"offer {offer_id} failed before money moved: {exc}; nothing charged",
                        {"offer_id": offer_id, "shop_id": offer["shop_id"]})
        _send_text(offer["wa_to"], "blocked",
                   "Something went wrong before any money moved, so nothing was charged. "
                   "The team can see exactly where it stopped.")
        return {"kind": "approve", "offer_id": offer_id, "blocked": "error"}

    _finish(offer_id, "approved", txn_ref=quote["txn_ref"])
    if offer["cart_id"]:
        with _db() as c:
            c.execute("DELETE FROM cart_activity WHERE cart_id = ?", (offer["cart_id"],))
    saved = quoted - charge
    saving_note = (f" A coupon landed since I messaged you, so it came to {money.rupees(charge)} "
                   f"instead of {money.rupees(quoted)} - {money.rupees(saved)} less."
                   if saved > 0 else "")
    chainlog.append("buyer", "whatsapp_checkout_paid",
                    f"offer {offer_id} paid: {money.rupees(charge)} to {offer['shop_id']} on "
                    f"{quote['txn_ref']}, confirmed={confirmed}. The shop charged exactly what "
                    "the approval said",
                    {"offer_id": offer_id, "txn_ref": quote["txn_ref"],
                     "shop_id": offer["shop_id"], "amount_paise": charge,
                     "payment_ref": result["payment_ref"], "confirmed": confirmed})
    _send_text(offer["wa_to"], "receipt",
               f"Paid {money.rupees(charge)} to {offer['shop_id']} (ref {quote['txn_ref']})."
               f"{saving_note} They charged exactly what you approved - the wallet checked.")
    return {"kind": "approve", "offer_id": offer_id, "paid": True,
            "txn_ref": quote["txn_ref"], "amount_paise": charge}


def _shop_refusal(resp: httpx.Response) -> errors.VelcrowError:
    try:
        p = resp.json()
    except ValueError:
        p = {}
    if p.get("code") == "OVER_CAP":
        # The tap-time mandate is capped at the quoted amount, so "over cap"
        # here MEANS "the basket now costs more than the message said".
        return errors.PriceChanged(
            "the basket now costs more than the message said",
            old_amount=0, new_amount=int(p.get("requested_paise", 0) or 0))
    err = errors.BadRequest(p.get("why", f"the shop refused (HTTP {resp.status_code})"))
    err.code = p.get("code", "SHOP_REFUSED")
    return err


def _requote(offer: dict[str, Any], exc: errors.PriceChanged) -> dict[str, Any]:
    """Never charge more than the message said - say the new number instead,
    with fresh buttons, and let the human decide again."""
    _finish(offer["offer_id"], "superseded", why=exc.why)
    new_amount = int(exc.payload().get("new_amount", 0) or 0)
    if new_amount <= 0:
        # We know the price moved but not to what; never quote a number we
        # don't have.
        chainlog.append("buyer", "whatsapp_requoted",
                        f"offer {offer['offer_id']} priced differently at tap time and no "
                        "fresh number was available; NOT charged, shopper sent back to the shop",
                        {"old_offer": offer["offer_id"], "shop_id": offer["shop_id"]})
        _send_text(offer["wa_to"], "requote_notice",
                   "The price changed between my message and your tap, so I did NOT pay. "
                   "Ask me on the shop page and I'll quote it fresh.")
        return {"kind": "approve", "offer_id": offer["offer_id"], "requoted_as": None}
    new_id = "wa_" + uuid.uuid4().hex[:12]
    with _db() as c:
        c.execute("INSERT INTO wa_offers (offer_id, kind, shop_id, shop_url, contact_key, "
                  "wa_to, shopper_ref, items, quoted_paise, cart_id, res_id, title, created_ts) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (new_id, offer["kind"], offer["shop_id"], offer["shop_url"],
                   offer["contact_key"], offer["wa_to"], offer["shopper_ref"], offer["items"],
                   new_amount, offer["cart_id"], offer["res_id"], offer["title"], time.time()))
    chainlog.append("buyer", "whatsapp_requoted",
                    f"offer {offer['offer_id']} priced differently at tap time "
                    f"({exc.why}); NOT charged - re-quoted as {new_id} at "
                    f"{money.rupees(new_amount)} for the human to decide again",
                    {"old_offer": offer["offer_id"], "new_offer": new_id,
                     "new_amount_paise": new_amount, "shop_id": offer["shop_id"]})
    _send_text(offer["wa_to"], "requote_notice",
               "The price changed between my message and your tap, so I did NOT pay.")
    _send_buttons(offer["wa_to"], "requote",
                  f"New total at {offer['shop_id']}: {money.rupees(new_amount)}. "
                  "Approve this exact amount, or Not now.",
                  [(f"apr_{new_id}", _approve_title(new_amount)),
                   (f"dec_{new_id}", "Not now")])
    return {"kind": "approve", "offer_id": offer["offer_id"], "requoted_as": new_id}


# -- WhatsApp login: phone + OTP, never a password ----------------------------
#
# The way a real Indian shop logs in - phone, six digits, in - with the code
# delivered by the SAME agent that will later message about carts and
# restocks. What it proves is ownership of the number, so reminders reach the
# right person. What it can NEVER do is move money: paying still takes the
# exact-amount approval through the wallet, logged in or not.
#
# The code is hashed at rest, lives five minutes, dies after three wrong
# attempts, and is never accepted anywhere but the verify endpoint. On the
# test tier Meta only delivers free-form messages inside a 24h window the
# shopper opened by messaging first; production would use an approved
# authentication template. Unconfigured transport lands the code in the
# outbox - honest, visible, undelivered.

def _code_hash(contact_key: str, code: str) -> str:
    return hashlib.sha256(f"{contact_key}:{code}".encode()).hexdigest()


def start_login(contact_raw: str) -> dict[str, Any]:
    try:
        key = contact.normalise(contact_raw)
    except contact.InvalidContact as exc:
        raise errors.BadRequest(str(exc))
    if not key.startswith("phone:"):
        raise errors.BadRequest(
            "WhatsApp login needs a phone number; an email still works as the "
            "no-password 'remember me'")
    wa_to = _reach(key)

    now = time.time()
    with _db() as c:
        row = c.execute("SELECT sends FROM wa_login_codes WHERE contact_key = ?",
                        (key,)).fetchone()
        sends = [t for t in (json.loads(row["sends"]) if row else [])
                 if t > now - LOGIN_RESEND_WINDOW]
        if len(sends) >= LOGIN_RESEND_MAX:
            raise errors.BadRequest(
                f"too many codes for this number; wait "
                f"{int((sends[0] + LOGIN_RESEND_WINDOW - now) // 60) + 1} minute(s)")
        code = f"{secrets.randbelow(10**6):06d}"
        c.execute("INSERT INTO wa_login_codes (contact_key, code_hash, expires_ts, "
                  "attempts, sends) VALUES (?, ?, ?, 0, ?) "
                  "ON CONFLICT(contact_key) DO UPDATE SET code_hash = excluded.code_hash, "
                  "expires_ts = excluded.expires_ts, attempts = 0, sends = excluded.sends",
                  (key, _code_hash(key, code), now + LOGIN_CODE_TTL,
                   json.dumps(sends + [now])))

    result = _send_text(wa_to, "login_code",
                        f"{code} is your login code. It expires in 5 minutes and only "
                        "works on the shop page you asked from. Nobody - including this "
                        "agent - will ever ask you to send it in chat.")
    chainlog.append("buyer", "login_code_sent",
                    f"login code for {_mask(wa_to)} sent ({result['mode']}); hashed at "
                    f"rest, {LOGIN_CODE_TTL}s expiry, {LOGIN_MAX_ATTEMPTS} attempts. "
                    "Owning this number unlocks recognition and reminders, never money",
                    {"contact_key": key, "mode": result["mode"]})
    return {"sent": result["mode"] != "failed", "mode": result["mode"],
            "expires_in_seconds": LOGIN_CODE_TTL}


LINK_LOGIN_TTL = 600


def create_link_login() -> dict[str, str]:
    """An out-of-band login session: the assistant gets only a link; the
    phone number and the code are entered on the person's own browser page
    and never pass through the chat."""
    link_id = "lnk_" + secrets.token_hex(12)
    with _db() as c:
        c.execute("INSERT INTO wa_link_logins (link_id, created_ts) VALUES (?, ?)",
                  (link_id, time.time()))
    return {"link_id": link_id}


def link_login_status(link_id: str) -> dict[str, Any]:
    with _db() as c:
        row = c.execute("SELECT * FROM wa_link_logins WHERE link_id = ?",
                        (link_id,)).fetchone()
    if row is None or time.time() > row["created_ts"] + LINK_LOGIN_TTL:
        return {"exists": False, "done": False}
    return {"exists": True, "done": row["status"] == "done",
            "contact": row["contact"] if row["status"] == "done" else "",
            "contact_key": row["contact_key"] if row["status"] == "done" else ""}


def verify_login(contact_raw: str, code: str, link_id: str = "") -> dict[str, Any]:
    try:
        key = contact.normalise(contact_raw)
    except contact.InvalidContact as exc:
        raise errors.BadRequest(str(exc))
    now = time.time()
    # Raising inside `with sqlite3.connect(...)` ROLLS BACK the transaction,
    # which would quietly undo the attempt count and the burn - so decide
    # first, commit the write, and only then raise (BREAKAGE.md).
    fail: errors.VelcrowError | None = None
    burned = False
    with _db() as c:
        row = c.execute("SELECT * FROM wa_login_codes WHERE contact_key = ?",
                        (key,)).fetchone()
        if row is None:
            fail = errors.MandateInvalid("no code was requested for this number",
                                         tier="login")
        elif now > row["expires_ts"]:
            c.execute("DELETE FROM wa_login_codes WHERE contact_key = ?", (key,))
            fail = errors.MandateExpired("that code has expired; request a fresh one",
                                         tier="login")
        elif not hmac.compare_digest(row["code_hash"], _code_hash(key, str(code).strip())):
            attempts = row["attempts"] + 1
            if attempts >= LOGIN_MAX_ATTEMPTS:
                c.execute("DELETE FROM wa_login_codes WHERE contact_key = ?", (key,))
                burned = True
                fail = errors.MandateInvalid(
                    "too many wrong attempts; that code is now dead - request a fresh one",
                    tier="login")
            else:
                c.execute("UPDATE wa_login_codes SET attempts = ? WHERE contact_key = ?",
                          (attempts, key))
                fail = errors.MandateInvalid(
                    f"wrong code ({LOGIN_MAX_ATTEMPTS - attempts} attempt(s) left)",
                    tier="login")
        else:
            c.execute("DELETE FROM wa_login_codes WHERE contact_key = ?", (key,))  # single use
    if fail is not None:
        if burned:
            chainlog.append("buyer", "login_code_burned",
                            f"{LOGIN_MAX_ATTEMPTS} wrong attempts on the code for "
                            f"{key[:9]}...; code destroyed, not brute-forced",
                            {"contact_key": key})
        raise fail
    chainlog.append("buyer", "login_verified",
                    f"ownership of {key[:9]}... proven by WhatsApp OTP. This unlocks "
                    "history and reminders on this device; money still needs the "
                    "exact-amount approval through the wallet",
                    {"contact_key": key})
    if link_id:
        # Out-of-band completion: ONLY a successful OTP verification can mark
        # a link session done - there is no separate "complete" call to forge.
        with _db() as c:
            c.execute("UPDATE wa_link_logins SET status = 'done', contact = ?, "
                      "contact_key = ? WHERE link_id = ? AND status = 'pending' "
                      "AND created_ts > ?",
                      (contact.display(contact_raw), key, link_id,
                       time.time() - LINK_LOGIN_TTL))
    return {"verified": True, "contact_key": key}


# -- read side for the console/audit ------------------------------------------

def outbox(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM wa_outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def offers(limit: int = 50) -> list[dict[str, Any]]:
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM wa_offers ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()]
    for r in rows:
        r["items"] = json.loads(r["items"])
        r["quoted_display"] = money.rupees(int(r["quoted_paise"]))
    return rows


def status() -> dict[str, Any]:
    return {
        "configured": whatsapp.configured(),
        "webhook_ready": bool(os.environ.get("WHATSAPP_APP_SECRET")
                              and os.environ.get("WHATSAPP_VERIFY_TOKEN")),
        "abandon_after_seconds": ABANDON_AFTER_SECONDS,
        "note": ("Transport is Meta's TEST number: it can only reach numbers its owner "
                 "verified, the same way Razorpay test mode moves no real rupees. "
                 "When unconfigured, every message lands in the outbox instead - "
                 "recorded, displayable, honestly undelivered."),
    }
