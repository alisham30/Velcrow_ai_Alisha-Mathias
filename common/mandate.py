"""Tier-1 session mandates (spec 4.1): HMAC-SHA256 JWTs with caps, allowed
shops, expiry and intent rules; SQLite revocation list; atomic spend
reservation against max_total.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import jwt

from common import errors

ALGO = "HS256"
REQUIRED_CLAIMS = ("jti", "exp", "max_total", "max_per_txn", "shops")


def _secret() -> str:
    secret = os.environ.get("MANDATE_SECRET", "")
    if not secret:
        raise RuntimeError("MANDATE_SECRET is not set (see .env)")
    return secret


def _db() -> sqlite3.Connection:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "trust.sqlite", timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS revoked (jti TEXT PRIMARY KEY, ts REAL NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS spends ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, jti TEXT NOT NULL, amount_paise INTEGER NOT NULL, ts REAL NOT NULL)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts REAL NOT NULL)")
    return conn


def default_rules() -> dict[str, Any]:
    return {"substitution_allowed": True, "exact_items": [], "payment_pref": "razorpay_test"}


def issue(
    max_total: int,
    max_per_txn: int,
    shops: list[str],
    ttl_seconds: int = 3600,
    rules: dict[str, Any] | None = None,
) -> str:
    """Issue a session mandate. All amounts are integer paise."""
    if not (isinstance(max_total, int) and isinstance(max_per_txn, int)):
        raise TypeError("caps must be integer paise")
    if max_total <= 0 or max_per_txn <= 0 or max_per_txn > max_total:
        raise ValueError("caps must be positive and max_per_txn <= max_total")
    now = int(time.time())
    claims: dict[str, Any] = {
        "typ": "mandate",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl_seconds,
        "max_total": max_total,
        "max_per_txn": max_per_txn,
        "shops": list(shops),
        "rules": rules if rules is not None else default_rules(),
    }
    return jwt.encode(claims, _secret(), algorithm=ALGO)


def verify(token: str) -> dict[str, Any]:
    """Signature, expiry, revocation, claim shape. Raises typed errors."""
    try:
        claims: dict[str, Any] = jwt.decode(token, _secret(), algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise errors.MandateExpired("mandate has expired")
    except jwt.InvalidTokenError as e:
        raise errors.MandateInvalid(f"mandate signature/format invalid: {e}")
    if claims.get("typ") != "mandate":
        raise errors.MandateInvalid("token is not a session mandate (wrong typ)")
    for c in REQUIRED_CLAIMS:
        if c not in claims:
            raise errors.MandateInvalid(f"mandate missing required claim '{c}'")
    if not (isinstance(claims["max_total"], int) and isinstance(claims["max_per_txn"], int)):
        raise errors.MandateInvalid("mandate caps must be integer paise")
    with _db() as conn:
        row = conn.execute("SELECT 1 FROM revoked WHERE jti = ?", (claims["jti"],)).fetchone()
    if row:
        raise errors.MandateInvalid("mandate has been revoked", jti=claims["jti"])
    return claims


def revoke(jti: str) -> None:
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO revoked (jti, ts) VALUES (?, ?)", (jti, time.time()))


def reserve_spend(jti: str, amount_paise: int, max_total: int) -> None:
    """Atomically reserve `amount_paise` against the mandate's max_total.

    max_total comes from verified (signed) claims. Raises OverCap and
    reserves nothing if the budget would be exceeded.
    """
    if not isinstance(amount_paise, int) or amount_paise <= 0:
        raise ValueError("amount must be positive integer paise")
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) FROM spends WHERE jti = ?", (jti,)
        ).fetchone()[0]
        if spent + amount_paise > max_total:
            conn.rollback()
            raise errors.OverCap(
                f"reserving {amount_paise} paise would exceed mandate max_total "
                f"({spent} already reserved of {max_total})",
                max_total=max_total, already_reserved_paise=spent, requested_paise=amount_paise,
            )
        conn.execute(
            "INSERT INTO spends (jti, amount_paise, ts) VALUES (?, ?, ?)", (jti, amount_paise, time.time())
        )
        conn.commit()
    finally:
        conn.close()


def release_spend(jti: str, amount_paise: int) -> None:
    """Release a prior reservation (recorded as a negative spend, auditable)."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO spends (jti, amount_paise, ts) VALUES (?, ?, ?)", (jti, -amount_paise, time.time())
        )


def spent(jti: str) -> int:
    with _db() as conn:
        return int(
            conn.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM spends WHERE jti = ?", (jti,)).fetchone()[0]
        )
