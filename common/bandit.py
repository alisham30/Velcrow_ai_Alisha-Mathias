"""Thompson sampling over proposal strategies (spec 4.6).

Beta(alpha, beta) per (shop, arm). An approved proposal is a success for its
arm, a rejected one a failure; sampling from each posterior and taking the
largest naturally trades exploration against exploitation. This is the one
place in the project where anything LEARNS from data - and it is a bandit
precisely because the sample sizes are tiny: at twenty-odd decisions a Beta
posterior genuinely updates, where anything bigger would be noise dressed as
inference.

Deliberately boring implementation: stdlib `random.betavariate`, no numpy,
state in the same trust.sqlite the revocation list lives in. The merchant can
see the counts, and the counts ARE the model.
"""
from __future__ import annotations

import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

# What the merchant agent may lead with (spec 4.6's arms, minus no_offer -
# "propose nothing" is a loop outcome, not a strategy to rank).
# price_alert stays as an arm so past decisions still read; the agent can no
# longer file one (it carried no checkable number and was used as a disguised
# "notify", found live). notify is the honest card for that.
ARMS = ("restock", "notify", "campaign", "coupon", "price_alert")


def _db() -> sqlite3.Connection:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "trust.sqlite", timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bandit ("
        "  shop_id TEXT NOT NULL, arm TEXT NOT NULL,"
        "  alpha INTEGER NOT NULL DEFAULT 1, beta INTEGER NOT NULL DEFAULT 1,"
        "  updated_ts REAL NOT NULL, PRIMARY KEY (shop_id, arm))"
    )
    return conn


def state(shop_id: str) -> dict[str, dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT arm, alpha, beta FROM bandit WHERE shop_id = ?", (shop_id,)).fetchall()
    known = {r[0]: {"alpha": r[1], "beta": r[2]} for r in rows}
    out: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        a, b = known.get(arm, {}).get("alpha", 1), known.get(arm, {}).get("beta", 1)
        out[arm] = {"alpha": a, "beta": b, "approvals": a - 1, "rejections": b - 1,
                    "mean": round(a / (a + b), 3)}
    return out


def record(shop_id: str, arm: str, accepted: bool) -> dict[str, Any]:
    """One observed decision. Approval bumps alpha, rejection bumps beta."""
    if arm not in ARMS:
        return {}
    with _db() as conn:
        conn.execute(
            "INSERT INTO bandit (shop_id, arm, alpha, beta, updated_ts)"
            " VALUES (?, ?, 1, 1, ?)"
            " ON CONFLICT(shop_id, arm) DO NOTHING", (shop_id, arm, time.time()))
        col = "alpha" if accepted else "beta"
        conn.execute(
            f"UPDATE bandit SET {col} = {col} + 1, updated_ts = ?"
            " WHERE shop_id = ? AND arm = ?", (time.time(), shop_id, arm))
    return state(shop_id)[arm]


def rank(shop_id: str, rng: random.Random | None = None) -> list[str]:
    """Arms ordered by one Thompson draw each - the order the agent should
    try strategies in this run. Seedable so tests are deterministic."""
    rng = rng or random.Random()
    posts = state(shop_id)
    draws = {arm: rng.betavariate(p["alpha"], p["beta"]) for arm, p in posts.items()}
    return sorted(ARMS, key=lambda a: -draws[a])
