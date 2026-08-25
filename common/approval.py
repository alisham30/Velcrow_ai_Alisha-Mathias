"""Tier-2 cart-bound approvals (spec 5.1): a short-lived HMAC JWT signed over
exactly one transaction — one merchant, one cart hash, one amount, one nonce.

The approval also carries the approved line items themselves so that, on a
mismatch, the log can name exactly which line changed (spec 5.1).
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

import jwt

from common import errors
from common.chainlog import canonical_json
from common.mandate import ALGO, _db, _secret

APPROVAL_TTL_SECONDS = 300


def canonical_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise line items to the fields that define the deal, sorted."""
    norm = [
        {
            "item_id": str(it["item_id"]),
            "variant": str(it.get("variant") or ""),
            "qty": int(it["qty"]),
            "unit_price_paise": int(it["unit_price_paise"]),
        }
        for it in items
    ]
    return sorted(norm, key=lambda it: (it["item_id"], it["variant"]))


def cart_hash(items: list[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(canonical_items(items)).encode("utf-8")).hexdigest()


def diff_lines(approved: list[dict[str, Any]], charged: list[dict[str, Any]]) -> str:
    """Human-readable statement of exactly which line(s) changed."""
    a = {(it["item_id"], it["variant"]): it for it in canonical_items(approved)}
    c = {(it["item_id"], it["variant"]): it for it in canonical_items(charged)}
    changes: list[str] = []
    for key in sorted(a.keys() | c.keys()):
        if key not in c:
            changes.append(f"line removed: {key[0]}/{key[1] or '-'}")
        elif key not in a:
            changes.append(f"line added: {key[0]}/{key[1] or '-'}")
        elif a[key] != c[key]:
            changes.append(
                f"line changed: {key[0]}/{key[1] or '-'} "
                f"approved qty={a[key]['qty']} @ {a[key]['unit_price_paise']}p, "
                f"charged qty={c[key]['qty']} @ {c[key]['unit_price_paise']}p"
            )
    return "; ".join(changes) or "no line-level difference found"


def issue(
    merchant: str,
    checkout_id: str,
    items: list[dict[str, Any]],
    amount_paise: int,
    session_jti: str,
    ttl_seconds: int = APPROVAL_TTL_SECONDS,
) -> str:
    """Sign an approval over THIS basket, from THIS merchant, at THIS price."""
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise ValueError("amount must be positive integer paise")
    now = int(time.time())
    norm = canonical_items(items)
    claims: dict[str, Any] = {
        "typ": "approval",
        "merchant": merchant,
        "checkout_id": checkout_id,
        "cart_hash": cart_hash(norm),
        "items": norm,
        "amount_paise": amount_paise,
        "currency": "INR",
        "session_jti": session_jti,
        "nonce": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, _secret(), algorithm=ALGO)


def verify(token: str, merchant: str, session_jti: str) -> dict[str, Any]:
    """Signature, expiry, typ, merchant + session binding, single-use nonce.

    Consumes the nonce: an approval that reaches verification is spent, even
    if a later wallet check fails (a changed cart needs a fresh approval).
    """
    try:
        claims: dict[str, Any] = jwt.decode(token, _secret(), algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise errors.MandateExpired("cart-bound approval has expired (5-minute window)", tier="approval")
    except jwt.InvalidTokenError as e:
        raise errors.MandateInvalid(f"approval signature/format invalid: {e}", tier="approval")
    if claims.get("typ") != "approval":
        raise errors.MandateInvalid("token is not a cart-bound approval (wrong typ)", tier="approval")
    if claims.get("merchant") != merchant:
        raise errors.MandateInvalid(
            f"approval is for merchant '{claims.get('merchant')}', not '{merchant}'", tier="approval"
        )
    if claims.get("session_jti") != session_jti:
        raise errors.MandateInvalid("approval is bound to a different session mandate", tier="approval")
    if claims.get("cart_hash") != cart_hash(claims.get("items", [])):
        raise errors.MandateInvalid("approval cart_hash does not match its own items", tier="approval")
    with _db() as conn:
        try:
            conn.execute("INSERT INTO nonces (nonce, ts) VALUES (?, ?)", (claims["nonce"], time.time()))
        except Exception:
            raise errors.MandateInvalid("approval nonce already used (replay refused)", tier="approval")
    return claims
