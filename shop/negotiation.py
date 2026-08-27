r"""Shop-side price negotiation (spec 16, phase 11): agent-to-agent commerce.

Two autonomous parties with opposed interests meet here. The buyer's agent
wants the lowest price its shopper's ceiling allows; this merchant wants the
most margin a sale will bear. Neither side's model is trusted with the
arithmetic: every rupee decision below is deterministic code, bounded by the
merchant's own cost book and margin floor - the same floor the growth agent's
`simulate_discount` refuses to cross, applied by the same rule.

The policy, in full (so a reader can predict every outcome):

    list   = the shelf price
    floor  = cost * (1 + floor_margin_pct/100), rounded up - the merchant's
             hard minimum. Selling below it loses money; no offer changes that.
    target = floor + half the headroom to list - what the merchant tries for.

    offer >= list        ACCEPT at list. A shop never charges above its shelf
                         price for being asked nicely.
    offer >= target      ACCEPT at the offer. Above target the merchant is
                         happy, and haggling further risks the sale.
    floor <= offer < target
                         COUNTER at target, signed and time-boxed. If the
                         buyer REPEATS an offer at or above the floor rather
                         than meeting the counter, the merchant closes the
                         deal at that offer - a sale at floor margin beats no
                         sale, and pretending otherwise would only teach
                         buyers to walk away.
    offer < floor        REFUSE, naming the floor and the minimum acceptable
                         price. Transparency is chosen deliberately: this
                         merchant would rather close a floor-priced sale than
                         protect a bluff, and a typed refusal is something the
                         buyer's agent can rank against other shops.

An ACCEPT or COUNTER is a signed price token (HMAC-SHA256 over the canonical
fields, the same primitive as the mandate), valid for OFFER_TTL_SECONDS and
redeemable exactly once at /order. Tamper with any field and the signature
dies; wait too long and the expiry does. Both sides log every step to their
own hash chain under one negotiation id, so the audit view can put the two
records side by side and show they agree - or exactly where they do not.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import chainlog, errors, mandate

OFFER_TTL_SECONDS = 120
DEFAULT_FLOOR_MARGIN_PCT = 12.0   # matches the growth agent's simulate_discount


def _sign(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    secret = os.environ.get("MANDATE_SECRET", "")
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def make_offer_token(shop_id: str, neg_id: str, item_id: str, variant: str, qty: int,
                     unit_price_paise: int, expires_ts: float) -> dict[str, Any]:
    fields = {"shop_id": shop_id, "neg_id": neg_id, "item_id": item_id, "variant": variant,
              "qty": qty, "unit_price_paise": unit_price_paise, "expires_ts": expires_ts}
    return {**fields, "sig": _sign(fields)}


def verify_offer_token(token: dict[str, Any], shop_id: str) -> dict[str, Any]:
    """Raises a typed error unless the token is intact, ours, and alive."""
    if not isinstance(token, dict) or "sig" not in token:
        raise errors.OfferInvalid("offer token is missing or malformed")
    fields = {k: v for k, v in token.items() if k != "sig"}
    if not hmac.compare_digest(_sign(fields), str(token.get("sig", ""))):
        raise errors.OfferInvalid(
            "offer token signature does not verify - a field was altered after signing")
    if fields.get("shop_id") != shop_id:
        raise errors.OfferInvalid("offer token was issued by a different shop",
                                  shop_id=shop_id)
    if time.time() > float(fields.get("expires_ts", 0)):
        raise errors.OfferInvalid(
            f"offer expired at {fields.get('expires_ts')}; negotiate again - counters are "
            f"time-boxed to {OFFER_TTL_SECONDS}s", expired=True)
    return fields


def mount(app: FastAPI, ctx: dict[str, Any]) -> None:
    db = ctx["db"]
    shop_id: str = ctx["shop_id"]
    cfg: dict[str, Any] = ctx["cfg"]
    json_body = ctx["json_body"]
    require_mandate = ctx["require_mandate"]
    floor_pct = float(cfg.get("floor_margin_pct", DEFAULT_FLOOR_MARGIN_PCT))

    def price_book(item_id: str, variant: str) -> tuple[dict[str, Any], int, int, int]:
        p = db.product(item_id)
        if p is None:
            raise errors.NotFound(f"no such product '{item_id}'", product_id=item_id)
        if db.stock_row(item_id, variant) is None:
            raise errors.NotFound(f"product '{item_id}' has no variant '{variant or '-'}'",
                                  product_id=item_id, variant=variant)
        list_price = int(p["price_paise"])
        cost = int(p.get("cost_price_paise", 0))
        # Integer arithmetic only: cost * 1.12 in floats yields 100800.0000001
        # for a 90,000-paise cost, and ceil turns that phantom fraction into a
        # rupee that exists nowhere. Money is integers everywhere else in this
        # project; the margin floor is no place for an exception.
        bp = 10_000 + round(floor_pct * 100)    # 12.0% -> 11200 basis points
        floor = -(-cost * bp // 10_000)         # ceiling division, all ints
        floor = min(floor, list_price)          # a floor above list is just list
        target = floor + (list_price - floor) // 2
        return p, list_price, floor, target

    @app.post("/negotiate")
    async def negotiate(request: Request) -> dict[str, Any]:
        """One round of the dance. Stateless for the caller: everything the
        merchant remembers about a negotiation is keyed by neg_id in its own
        database, and everything it promises is inside the signed token."""
        body = await json_body(request)
        claims = require_mandate(request)

        item_id = str(body.get("item_id") or "")
        variant = str(body.get("variant") or "")
        qty = int(body.get("qty", 1))
        offer = int(body.get("offer_paise", 0))     # per unit
        if qty <= 0 or offer <= 0:
            raise errors.BadRequest("qty and offer_paise must be positive integers")
        neg_id = str(body.get("neg_id") or "") or "neg_" + uuid.uuid4().hex[:12]

        p, list_price, floor, target = price_book(item_id, variant)
        prior = db.negotiation_round(neg_id)
        db.record_negotiation_round(neg_id, item_id, variant, qty, offer)

        def token_for(price: int) -> dict[str, Any]:
            return make_offer_token(shop_id, neg_id, item_id, variant, qty, price,
                                    time.time() + OFFER_TTL_SECONDS)

        # -- the policy, exactly as the module docstring states it ----------
        if offer >= list_price:
            decision, price = "accepted", list_price
            why = (f"offered {offer} against a list price of {list_price}; accepted at list - "
                   "this shop does not charge above its shelf price")
        elif offer >= target:
            decision, price = "accepted", offer
            why = (f"offer {offer} meets the target ({target}); accepted - margin is "
                   "comfortable and haggling further risks the sale")
        elif offer >= floor and prior and int(prior["offer_paise"]) >= offer:
            # The buyer heard the counter and held their ground. Close it.
            decision, price = "accepted", max(offer, int(prior["offer_paise"]))
            why = (f"repeated offer {offer} at or above the floor ({floor}); accepted - a "
                   "floor-priced sale beats no sale")
        elif offer >= floor:
            decision, price = "counter", target
            why = (f"offer {offer} clears the floor ({floor}) but not the target ({target}); "
                   f"countered at {target}, signed, valid {OFFER_TTL_SECONDS}s")
        else:
            decision, price = "refused", 0
            why = (f"offer {offer} is below this merchant's floor: cost "
                   f"{p.get('cost_price_paise', 0)} + {floor_pct}% margin = {floor} minimum "
                   "per unit. Nothing sells at a loss, whoever is asking")

        chainlog.append(shop_id, f"negotiation_{decision}",
                        f"negotiation {neg_id}: '{p['name']}'"
                        f"{' [' + variant + ']' if variant else ''} x{qty} - {why}",
                        {"neg_id": neg_id, "product_id": item_id, "variant": variant,
                         "qty": qty, "offer_paise": offer, "decision": decision,
                         "unit_price_paise": price, "list_paise": list_price,
                         "floor_paise": floor, "target_paise": target,
                         "jti": claims["jti"]})

        out: dict[str, Any] = {
            "neg_id": neg_id, "decision": decision, "why": why,
            "item_id": item_id, "variant": variant, "qty": qty,
            "list_paise": list_price,
        }
        if decision == "refused":
            out["floor"] = {"floor_margin_pct": floor_pct, "minimum_unit_paise": floor,
                            "note": "offers at or above this close at floor margin"}
        else:
            out["unit_price_paise"] = price
            out["offer_token"] = token_for(price)
            out["expires_in_seconds"] = OFFER_TTL_SECONDS
            out["redeem"] = ("POST /order with {cart_id, offer_token} - the cart must hold "
                             "exactly the negotiated line")
        return out


def manifest_block() -> dict[str, Any]:
    return {
        "endpoint": "POST /negotiate",
        "body": {"item_id": "<id>", "variant": "<label|empty>", "qty": "<int>",
                 "offer_paise": "<per-unit offer>", "neg_id": "<omit on first round>"},
        "auth": "Authorization: Mandate <jwt> - same mandate as ordering",
        "outcomes": ["accepted", "counter", "refused"],
        "offer_token": {"redeem_at": "POST /order", "field": "offer_token",
                        "ttl_seconds": OFFER_TTL_SECONDS,
                        "signed": "HMAC-SHA256 over the canonical fields; any alteration "
                                  "kills the signature"},
        "refusals": "typed, with the margin floor and minimum acceptable price named",
    }
