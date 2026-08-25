"""SQLite persistence for one shop. Products/stock seeded from the catalog
file; carts, orders (with stock holds), reservations, idempotency records.
Flat-stock products use variant '' internally. All money integer paise.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

PRICE_LOCK_SECONDS = 300  # manifest policies.price_lock_seconds


class ShopDB:
    def __init__(self, shop_id: str, catalog: list[dict[str, Any]]) -> None:
        self.shop_id = shop_id
        self.catalog = {p["id"]: p for p in catalog}
        d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"shop_{shop_id}.sqlite"
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS stock (
                  product_id TEXT NOT NULL, variant TEXT NOT NULL,
                  stock INTEGER NOT NULL, restock_date TEXT,
                  PRIMARY KEY (product_id, variant));
                CREATE TABLE IF NOT EXISTS carts (cart_id TEXT PRIMARY KEY, created_ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cart_items (
                  cart_id TEXT NOT NULL, line_id TEXT NOT NULL,
                  item_id TEXT NOT NULL, variant TEXT NOT NULL, qty INTEGER NOT NULL,
                  PRIMARY KEY (cart_id, line_id));
                CREATE TABLE IF NOT EXISTS orders (
                  txn_ref TEXT PRIMARY KEY, cart_id TEXT NOT NULL, status TEXT NOT NULL,
                  charge_amount INTEGER NOT NULL, line_items TEXT NOT NULL, coupon TEXT NOT NULL,
                  mandate_jti TEXT NOT NULL, created_ts REAL NOT NULL, expires_ts REAL NOT NULL,
                  razorpay_order_id TEXT, payment_ref TEXT);
                CREATE TABLE IF NOT EXISTS reservations (
                  res_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, variant TEXT NOT NULL,
                  contact_ref TEXT NOT NULL, mandate_jti TEXT NOT NULL,
                  status TEXT NOT NULL, created_ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency (
                  key TEXT NOT NULL, endpoint TEXT NOT NULL, request_hash TEXT NOT NULL,
                  status INTEGER NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY (key, endpoint));
                """
            )
        self._seed(reset=os.environ.get("VELCROW_RESET") == "1")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _seed(self, reset: bool) -> None:
        with self._conn() as c:
            if reset:
                c.execute("DELETE FROM stock")
            for p in self.catalog.values():
                variants = p.get("variants") or [{"label": "", "stock": p.get("stock", 0),
                                                 "restock_date": p.get("restock_date")}]
                for v in variants:
                    c.execute(
                        "INSERT OR IGNORE INTO stock (product_id, variant, stock, restock_date)"
                        " VALUES (?, ?, ?, ?)",
                        (p["id"], v["label"], int(v["stock"]), v.get("restock_date")),
                    )

    # -- products / stock ---------------------------------------------------
    def product(self, item_id: str) -> dict[str, Any] | None:
        return self.catalog.get(item_id)

    def stock_row(self, item_id: str, variant: str) -> tuple[int, str | None] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT stock, restock_date FROM stock WHERE product_id = ? AND variant = ?",
                (item_id, variant),
            ).fetchone()
        return (row["stock"], row["restock_date"]) if row else None

    def stock_map(self, item_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT variant, stock, restock_date FROM stock WHERE product_id = ?", (item_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def adjust_stock(self, item_id: str, variant: str, delta: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE stock SET stock = stock + ? WHERE product_id = ? AND variant = ?",
                (delta, item_id, variant),
            )

    # -- carts --------------------------------------------------------------
    def create_cart(self) -> str:
        cart_id = "cart_" + uuid.uuid4().hex[:10]
        with self._conn() as c:
            c.execute("INSERT INTO carts (cart_id, created_ts) VALUES (?, ?)", (cart_id, time.time()))
        return cart_id

    def cart_exists(self, cart_id: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM carts WHERE cart_id = ?", (cart_id,)).fetchone() is not None

    def cart_items(self, cart_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT line_id, item_id, variant, qty FROM cart_items WHERE cart_id = ? ORDER BY rowid",
                (cart_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_line(self, cart_id: str, item_id: str, variant: str, qty: int) -> str:
        """Add qty to an existing matching line or create a new one."""
        with self._conn() as c:
            row = c.execute(
                "SELECT line_id, qty FROM cart_items WHERE cart_id = ? AND item_id = ? AND variant = ?",
                (cart_id, item_id, variant),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE cart_items SET qty = ? WHERE cart_id = ? AND line_id = ?",
                    (row["qty"] + qty, cart_id, row["line_id"]),
                )
                return str(row["line_id"])
            line_id = "line_" + uuid.uuid4().hex[:8]
            c.execute(
                "INSERT INTO cart_items (cart_id, line_id, item_id, variant, qty) VALUES (?, ?, ?, ?, ?)",
                (cart_id, line_id, item_id, variant, qty),
            )
            return line_id

    def get_line(self, cart_id: str, line_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT line_id, item_id, variant, qty FROM cart_items WHERE cart_id = ? AND line_id = ?",
                (cart_id, line_id),
            ).fetchone()
        return dict(row) if row else None

    def set_line_qty(self, cart_id: str, line_id: str, qty: int) -> None:
        with self._conn() as c:
            if qty <= 0:
                c.execute("DELETE FROM cart_items WHERE cart_id = ? AND line_id = ?", (cart_id, line_id))
            else:
                c.execute(
                    "UPDATE cart_items SET qty = ? WHERE cart_id = ? AND line_id = ?", (qty, cart_id, line_id)
                )

    # -- orders -------------------------------------------------------------
    def create_order(self, cart_id: str, charge_amount: int, line_items: list[dict[str, Any]],
                     coupon: dict[str, Any], mandate_jti: str) -> dict[str, Any]:
        txn_ref = "txn_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders (txn_ref, cart_id, status, charge_amount, line_items, coupon,"
                " mandate_jti, created_ts, expires_ts) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (txn_ref, cart_id, charge_amount, json.dumps(line_items), json.dumps(coupon),
                 mandate_jti, now, now + PRICE_LOCK_SECONDS),
            )
        return self.get_order(txn_ref)  # type: ignore[return-value]

    def get_order(self, txn_ref: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE txn_ref = ?", (txn_ref,)).fetchone()
        if not row:
            return None
        order = dict(row)
        order["line_items"] = json.loads(order["line_items"])
        order["coupon"] = json.loads(order["coupon"])
        return order

    def set_order_status(self, txn_ref: str, status: str,
                         razorpay_order_id: str | None = None, payment_ref: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE orders SET status = ?,"
                " razorpay_order_id = COALESCE(?, razorpay_order_id),"
                " payment_ref = COALESCE(?, payment_ref) WHERE txn_ref = ?",
                (status, razorpay_order_id, payment_ref, txn_ref),
            )

    # -- reservations -------------------------------------------------------
    def create_reservation(self, item_id: str, variant: str, contact_ref: str, mandate_jti: str) -> str:
        res_id = "res_" + uuid.uuid4().hex[:10]
        with self._conn() as c:
            c.execute(
                "INSERT INTO reservations (res_id, item_id, variant, contact_ref, mandate_jti, status,"
                " created_ts) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (res_id, item_id, variant, contact_ref, mandate_jti, time.time()),
            )
        return res_id

    # -- idempotency --------------------------------------------------------
    def idem_get(self, key: str, endpoint: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT request_hash, status, body FROM idempotency WHERE key = ? AND endpoint = ?",
                (key, endpoint),
            ).fetchone()
        return dict(row) if row else None

    def idem_put(self, key: str, endpoint: str, request_hash: str, status: int, body: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO idempotency (key, endpoint, request_hash, status, body)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, endpoint, request_hash, status, body),
            )
