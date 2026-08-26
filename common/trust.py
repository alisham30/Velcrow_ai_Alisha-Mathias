"""Per-shop trust, AIMD (spec 4.5).

Additive increase, multiplicative decrease: a clean deal earns +0.05 up to a
ceiling of 1.0; a violation halves the score. That asymmetry is the point -
trust is slow to build and fast to lose, so one caught lie costs a merchant
more than a dozen honest transactions earned them.

Persisted, because a trust score that resets when a process restarts is not a
trust score. Starts at 0.7 for a shop nobody has dealt with yet: willing, not
credulous.

Feeds the buyer's ranking (spec 4.4) as the 0.2 weight, so a merchant caught
inflating a charge is visibly outranked afterwards rather than merely logged.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

START = 0.7
CEILING = 1.0
FLOOR = 0.05
CLEAN_DEAL = 0.05
VIOLATION_FACTOR = 0.5

# Why a score moved. Anything not "clean" halves it.
VIOLATIONS = ("price_mismatch", "invalid_mandate", "injection", "cheat_detected")


def _db() -> sqlite3.Connection:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "trust.sqlite", timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trust ("
        "  shop_id TEXT PRIMARY KEY, score REAL NOT NULL, deals INTEGER NOT NULL DEFAULT 0,"
        "  violations INTEGER NOT NULL DEFAULT 0, updated_ts REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trust_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL, kind TEXT NOT NULL,"
        "  why TEXT NOT NULL, score_before REAL NOT NULL, score_after REAL NOT NULL,"
        "  ts REAL NOT NULL)"
    )
    return conn


def score(shop_id: str) -> float:
    with _db() as conn:
        row = conn.execute("SELECT score FROM trust WHERE shop_id = ?", (shop_id,)).fetchone()
    return float(row[0]) if row else START


def record(shop_id: str, kind: str, why: str) -> dict[str, Any]:
    """Move a shop's score and say why. `kind` is 'clean' or a VIOLATIONS entry."""
    before = score(shop_id)
    if kind == "clean":
        after = min(CEILING, before + CLEAN_DEAL)
    else:
        after = max(FLOOR, before * VIOLATION_FACTOR)
    now = time.time()
    with _db() as conn:
        conn.execute(
            "INSERT INTO trust (shop_id, score, deals, violations, updated_ts)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(shop_id) DO UPDATE SET score = excluded.score,"
            "   deals = trust.deals + excluded.deals,"
            "   violations = trust.violations + excluded.violations,"
            "   updated_ts = excluded.updated_ts",
            (shop_id, after, 1 if kind == "clean" else 0, 0 if kind == "clean" else 1, now),
        )
        conn.execute(
            "INSERT INTO trust_events (shop_id, kind, why, score_before, score_after, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (shop_id, kind, why, before, after, now),
        )
    return {"shop_id": shop_id, "kind": kind, "why": why,
            "score_before": round(before, 3), "score_after": round(after, 3)}


def all_scores() -> dict[str, dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT shop_id, score, deals, violations FROM trust ORDER BY shop_id").fetchall()
    return {r[0]: {"score": round(float(r[1]), 3), "deals": r[2], "violations": r[3]}
            for r in rows}


def history(shop_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    with _db() as conn:
        if shop_id:
            rows = conn.execute(
                "SELECT shop_id, kind, why, score_before, score_after, ts FROM trust_events"
                " WHERE shop_id = ? ORDER BY id DESC LIMIT ?", (shop_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT shop_id, kind, why, score_before, score_after, ts FROM trust_events"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"shop_id": r[0], "kind": r[1], "why": r[2],
             "score_before": round(float(r[3]), 3), "score_after": round(float(r[4]), 3),
             "ts": r[5]} for r in rows]
