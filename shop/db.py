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
                CREATE TABLE IF NOT EXISTS demand_ledger (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
                  variant TEXT NOT NULL, qty INTEGER NOT NULL,
                  unit_price_paise INTEGER NOT NULL, value_paise INTEGER NOT NULL,
                  reason TEXT NOT NULL, res_id TEXT, created_ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency (
                  key TEXT NOT NULL, endpoint TEXT NOT NULL, request_hash TEXT NOT NULL,
                  status INTEGER NOT NULL, body TEXT NOT NULL,
                  PRIMARY KEY (key, endpoint));
                """
            )
        self._migrate()
        self._seed(reset=os.environ.get("VELCROW_RESET") == "1")

    def _migrate(self) -> None:
        """Additive columns only. `shopper_ref` arrived with reorder (spec 7.3):
        a browser-held reference, not an account (spec 14 bans accounts), so a
        returning shopper's last basket can be found without anyone logging in.
        Orders placed before it exist with an empty ref and are simply never
        matched by a reorder lookup."""
        with self._conn() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
            if "shopper_ref" not in cols:
                c.execute("ALTER TABLE orders ADD COLUMN shopper_ref TEXT NOT NULL DEFAULT ''")
            # A reservation records a contact_ref the shopper typed, which is
            # who to contact. shopper_ref is *where* to reach them - the browser
            # running the widget - so a restock can put the offer in front of
            # them instead of waiting for them to come back and ask.
            res_cols = {r["name"] for r in c.execute("PRAGMA table_info(reservations)")}
            if "shopper_ref" not in res_cols:
                c.execute("ALTER TABLE reservations ADD COLUMN shopper_ref TEXT NOT NULL DEFAULT ''")
            if "qty" not in res_cols:
                c.execute("ALTER TABLE reservations ADD COLUMN qty INTEGER NOT NULL DEFAULT 1")

            # The portable half of shopper identity (spec 7.3). shopper_ref is
            # per-browser and dies with the browser; contact_key is the thing
            # the shopper can retype on a new device. Orders carry both, and
            # shopper_links maps the many browsers one person uses onto the one
            # contact, so history follows the person rather than the device.
            if "contact_key" not in cols:
                c.execute("ALTER TABLE orders ADD COLUMN contact_key TEXT NOT NULL DEFAULT ''")
            if "contact_ref" not in cols:
                c.execute("ALTER TABLE orders ADD COLUMN contact_ref TEXT NOT NULL DEFAULT ''")
            if "contact_key" not in res_cols:
                c.execute("ALTER TABLE reservations ADD COLUMN contact_key TEXT NOT NULL DEFAULT ''")
            c.execute(
                "CREATE TABLE IF NOT EXISTS shopper_links ("
                "  contact_key TEXT NOT NULL, shopper_ref TEXT NOT NULL,"
                "  contact_ref TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL,"
                "  PRIMARY KEY (contact_key, shopper_ref))"
            )
            # What the merchant console measures (spec 6.1, 7.4). `assisted` is
            # set when the agent drove the checkout rather than the storefront
            # form; `rescued` when the basket came back from a reservation that
            # had been refused for stock. Both are written at purchase time -
            # a metric reconstructed later is a metric nobody trusts.
            if "assisted" not in cols:
                c.execute("ALTER TABLE orders ADD COLUMN assisted INTEGER NOT NULL DEFAULT 0")
            if "rescued" not in cols:
                c.execute("ALTER TABLE orders ADD COLUMN rescued INTEGER NOT NULL DEFAULT 0")
            c.execute("CREATE INDEX IF NOT EXISTS idx_orders_contact"
                      " ON orders (contact_key, status, created_ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_links_ref"
                      " ON shopper_links (shopper_ref)")
            # The autonomous merchant agent (spec 7.5). A proposal is a card in
            # the console, never an applied change: approval is what applies it,
            # rejection feeds the bandit, and both are chain-logged.
            c.execute(
                "CREATE TABLE IF NOT EXISTS proposals ("
                "  prop_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,"
                "  rationale TEXT NOT NULL, numbers TEXT NOT NULL,"
                "  status TEXT NOT NULL DEFAULT 'open',"
                "  decided_reason TEXT NOT NULL DEFAULT '',"
                "  created_ts REAL NOT NULL, decided_ts REAL)"
            )
            # Coupons an approved campaign creates at runtime, merged with the
            # config set until they lapse.
            # WHO was refused, not just what. Without this a shop that cannot
            # hold a unit has nobody to tell when the item comes back, so the
            # sale dies even though we knew who wanted it.
            dl_cols = {r["name"] for r in c.execute("PRAGMA table_info(demand_ledger)")}
            if "shopper_ref" not in dl_cols:
                c.execute("ALTER TABLE demand_ledger ADD COLUMN shopper_ref TEXT NOT NULL DEFAULT ''")
            if "contact_key" not in dl_cols:
                c.execute("ALTER TABLE demand_ledger ADD COLUMN contact_key TEXT NOT NULL DEFAULT ''")
            if "notified_ts" not in dl_cols:
                c.execute("ALTER TABLE demand_ledger ADD COLUMN notified_ts REAL")
            # When the shopper comes back and buys, the refusal is over. Without
            # this the ledger never closed: a sale we had actually recovered went
            # on being counted as demand, at a shop that takes no reservations.
            if "converted_ts" not in dl_cols:
                c.execute("ALTER TABLE demand_ledger ADD COLUMN converted_ts REAL")
            if "converted_txn" not in dl_cols:
                c.execute("ALTER TABLE demand_ledger ADD COLUMN converted_txn TEXT NOT NULL"
                          " DEFAULT ''")
            c.execute(
                "CREATE TABLE IF NOT EXISTS runtime_coupons ("
                "  code TEXT PRIMARY KEY, coupon TEXT NOT NULL,"
                "  expires_ts REAL NOT NULL, created_ts REAL NOT NULL)"
            )

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

    def clear_cart(self, cart_id: str) -> int:
        """Empty a cart and report how many lines went. Called when an order is
        confirmed paid: that basket has been bought, so it stops being a basket.
        Deliberately NOT called when an order is merely created or expires - a
        shopper whose payment never completed still owns their basket."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))
            return cur.rowcount

    # -- orders -------------------------------------------------------------
    def create_order(self, cart_id: str, charge_amount: int, line_items: list[dict[str, Any]],
                     coupon: dict[str, Any], mandate_jti: str, shopper_ref: str = "",
                     contact_key: str = "", contact_ref: str = "",
                     assisted: bool = False) -> dict[str, Any]:
        txn_ref = "txn_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO orders (txn_ref, cart_id, status, charge_amount, line_items, coupon,"
                " mandate_jti, created_ts, expires_ts, shopper_ref, contact_key, contact_ref,"
                " assisted) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (txn_ref, cart_id, charge_amount, json.dumps(line_items), json.dumps(coupon),
                 mandate_jti, now, now + PRICE_LOCK_SECONDS, shopper_ref, contact_key, contact_ref,
                 1 if assisted else 0),
            )
        return self.get_order(txn_ref)  # type: ignore[return-value]

    def convert_reservations(self, order: dict[str, Any]) -> list[str]:
        """Close any reservation this paid basket satisfies, and report them.

        A rescued sale is one the shop had already refused for stock: the
        shopper was turned away, reserved, was told it was back, and bought.
        Matched on (item, variant) for the same shopper so the claim is derived
        from what actually happened rather than asserted by the agent.
        """
        refs = {order.get("shopper_ref") or ""} | set(
            self.refs_for_contact(order.get("contact_key") or ""))
        refs.discard("")
        contact_key = order.get("contact_key") or ""
        if not refs and not contact_key:
            return []

        converted: list[str] = []
        with self._conn() as c:
            for li in order["line_items"]:
                clauses, params = [], [li["item_id"], str(li.get("variant") or "")]
                if refs:
                    clauses.append(f"shopper_ref IN ({','.join('?' * len(refs))})")
                    params.extend(sorted(refs))
                if contact_key:
                    clauses.append("contact_key = ?")
                    params.append(contact_key)
                rows = c.execute(
                    "SELECT res_id FROM reservations WHERE item_id = ? AND variant = ?"
                    f" AND status IN ('open','notified') AND ({' OR '.join(clauses)})", params
                ).fetchall()
                for r in rows:
                    c.execute("UPDATE reservations SET status = 'converted' WHERE res_id = ?",
                              (r["res_id"],))
                    converted.append(r["res_id"])
        if converted:
            self.mark_order_rescued(order["txn_ref"])
        return converted

    def mark_order_rescued(self, txn_ref: str) -> None:
        """One flag, one meaning: this order recovered a sale the shop had
        already refused. Set from the reservation or the ledger, never from
        the caller's say-so."""
        with self._conn() as c:
            c.execute("UPDATE orders SET rescued = 1 WHERE txn_ref = ?", (txn_ref,))

    def convert_demand(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        """Close ledger rows this paid basket answers, and report what they were.

        `convert_reservations` only sees the reservations table, so at a shop
        that cannot hold a unit - FreshKart takes no reservations - a shopper
        who was refused, told when it landed, and came back and paid produced a
        rescued sale that nothing recorded. The ledger went on reporting their
        money as lost while it was sitting in the orders table.

        Matched the same way a reservation is: same item and variant, same
        shopper (by ref or by contact), so the claim is derived from what
        happened rather than asserted by whoever placed the order.
        """
        refs = {order.get("shopper_ref") or ""} | set(
            self.refs_for_contact(order.get("contact_key") or ""))
        refs.discard("")
        contact_key = order.get("contact_key") or ""
        if not refs and not contact_key:
            return []

        closed: list[dict[str, Any]] = []
        with self._conn() as c:
            for li in order["line_items"]:
                clauses, params = [], [li["item_id"], str(li.get("variant") or "")]
                if refs:
                    clauses.append(f"shopper_ref IN ({','.join('?' * len(refs))})")
                    params.extend(sorted(refs))
                if contact_key:
                    clauses.append("contact_key = ?")
                    params.append(contact_key)
                rows = c.execute(
                    "SELECT id, qty, value_paise FROM demand_ledger WHERE item_id = ?"
                    " AND variant = ? AND converted_ts IS NULL"
                    f" AND ({' OR '.join(clauses)}) ORDER BY created_ts", params).fetchall()
                # Only as much as this basket actually supplies. Buying 1 of
                # something you were refused 4 of recovers one unit of demand,
                # not the whole refusal.
                remaining = int(li.get("qty", 0))
                for r in rows:
                    if remaining <= 0:
                        break
                    if int(r["qty"]) > remaining:
                        continue
                    c.execute("UPDATE demand_ledger SET converted_ts = ?, converted_txn = ?"
                              " WHERE id = ?", (time.time(), order["txn_ref"], r["id"]))
                    remaining -= int(r["qty"])
                    closed.append({"id": int(r["id"]), "item_id": li["item_id"],
                                   "variant": str(li.get("variant") or ""),
                                   "qty": int(r["qty"]), "value_paise": int(r["value_paise"])})
        return closed

    def summary(self) -> dict[str, Any]:
        """What this merchant's console reports (spec 6.1). Paid orders only -
        a quote nobody approved is not revenue."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS orders, COALESCE(SUM(charge_amount), 0) AS revenue,"
                " COALESCE(SUM(assisted), 0) AS assisted_orders,"
                " COALESCE(SUM(CASE WHEN assisted = 1 THEN charge_amount ELSE 0 END), 0)"
                "   AS assisted_revenue,"
                " COALESCE(SUM(rescued), 0) AS rescued_orders,"
                " COALESCE(SUM(CASE WHEN rescued = 1 THEN charge_amount ELSE 0 END), 0)"
                "   AS rescued_revenue,"
                " COALESCE(SUM(CASE WHEN coupon LIKE '%\"codes\": [\"%' THEN 1 ELSE 0 END), 0)"
                "   AS with_coupon"
                " FROM orders WHERE status = 'paid'"
            ).fetchone()
            reservations = c.execute(
                "SELECT status, COUNT(*) AS n FROM reservations GROUP BY status").fetchall()
        s = dict(row)
        s["aov_paise"] = s["revenue"] // s["orders"] if s["orders"] else 0
        s["unassisted_orders"] = s["orders"] - s["assisted_orders"]
        s["unassisted_revenue"] = s["revenue"] - s["assisted_revenue"]
        s["coupon_claim_rate"] = round(s["with_coupon"] / s["orders"], 3) if s["orders"] else 0.0
        s["reservations"] = {r["status"]: r["n"] for r in reservations}
        return s

    # -- shopper identity (spec 7.3) ----------------------------------------
    def link_shopper(self, contact_key: str, shopper_ref: str, contact_ref: str = "") -> int:
        """Bind a browser to a contact and claim that browser's past orders.

        Returns how many previously anonymous orders were claimed. This is what
        makes the key work retroactively: someone who bought before ever giving
        a contact still finds that basket the moment they give one.
        """
        if not contact_key:
            return 0
        with self._conn() as c:
            if shopper_ref:
                c.execute(
                    "INSERT OR IGNORE INTO shopper_links (contact_key, shopper_ref, contact_ref,"
                    " first_seen) VALUES (?, ?, ?, ?)",
                    (contact_key, shopper_ref, contact_ref, time.time()),
                )
                claimed = c.execute(
                    "UPDATE orders SET contact_key = ?, contact_ref = ?"
                    " WHERE shopper_ref = ? AND contact_key = ''",
                    (contact_key, contact_ref, shopper_ref),
                ).rowcount
                c.execute(
                    "UPDATE reservations SET contact_key = ?"
                    " WHERE shopper_ref = ? AND contact_key = ''",
                    (contact_key, shopper_ref),
                )
                return int(claimed)
        return 0

    def refs_for_contact(self, contact_key: str) -> list[str]:
        if not contact_key:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT shopper_ref FROM shopper_links WHERE contact_key = ?", (contact_key,)
            ).fetchall()
        return [r["shopper_ref"] for r in rows]

    def last_paid_order(self, shopper_ref: str = "", contact_key: str = "") -> dict[str, Any] | None:
        """The most recent completed order for this shopper - what "my usual
        order" reorders (spec 6.3, 7.3). Only paid orders count: a pending or
        expired one was never a basket the shopper actually bought.

        Resolved across BOTH halves of identity: the contact key directly, plus
        every browser ref ever linked to it, plus the ref in front of us. A
        shopper who bought on a laptop and asks on a phone is one person here.
        """
        refs = set(self.refs_for_contact(contact_key))
        if shopper_ref:
            refs.add(shopper_ref)
        if not refs and not contact_key:
            return None

        # An order already claimed by a DIFFERENT contact is never mine, even
        # when it was placed on this browser. Without that guard, signing in
        # with your own email on a device someone else used hands you their
        # order history - the browser ref is shared, the person is not.
        clauses: list[str] = []
        params: list[Any] = []
        if contact_key:
            clauses.append("contact_key = ?")
            params.append(contact_key)
        if refs:
            placeholders = ",".join("?" * len(refs))
            clauses.append(
                f"(shopper_ref IN ({placeholders}) AND contact_key = '')")
            params.extend(sorted(refs))

        with self._conn() as c:
            row = c.execute(
                f"SELECT * FROM orders WHERE ({' OR '.join(clauses)}) AND status = 'paid'"
                " ORDER BY created_ts DESC LIMIT 1", params).fetchone()
        if not row:
            return None
        order = dict(row)
        order["line_items"] = json.loads(order["line_items"])
        order["coupon"] = json.loads(order["coupon"])
        return order

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
    def create_reservation(self, item_id: str, variant: str, contact_ref: str, mandate_jti: str,
                           shopper_ref: str = "", qty: int = 1, contact_key: str = "") -> str:
        res_id = "res_" + uuid.uuid4().hex[:10]
        with self._conn() as c:
            c.execute(
                "INSERT INTO reservations (res_id, item_id, variant, contact_ref, mandate_jti, status,"
                " created_ts, shopper_ref, qty, contact_key)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (res_id, item_id, variant, contact_ref, mandate_jti, time.time(), shopper_ref, qty,
                 contact_key),
            )
        return res_id

    def open_reservations(self, item_id: str, variant: str) -> list[dict[str, Any]]:
        """Everyone still waiting on this variant, oldest first - a restock is
        honoured in the order people asked."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT res_id, item_id, variant, contact_ref, contact_key, shopper_ref,"
                " mandate_jti, qty, status, created_ts FROM reservations"
                " WHERE item_id = ? AND variant = ? AND status = 'open' ORDER BY created_ts",
                (item_id, variant),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_reservation_status(self, res_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE reservations SET status = ? WHERE res_id = ?", (status, res_id))

    def reservation(self, res_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM reservations WHERE res_id = ?", (res_id,)).fetchone()
        return dict(row) if row else None

    def all_reservations(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT res_id, item_id, variant, contact_ref, shopper_ref, qty, status, created_ts"
                " FROM reservations ORDER BY created_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- demand ledger (spec 6.1, 7.2: refusals become restock forecasts) ----
    def record_lost_demand(self, item_id: str, variant: str, qty: int, unit_price_paise: int,
                           reason: str, res_id: str | None = None, shopper_ref: str = "",
                           contact_key: str = "") -> int:
        """One row per refused demand. value_paise is the revenue not taken.

        Records WHO was turned away where we know. A shop that cannot HOLD a
        unit can still TELL someone it has landed - those are different
        capabilities (spec 6.1), and treating them as one threw the sale away.
        """
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO demand_ledger (item_id, variant, qty, unit_price_paise, value_paise,"
                " reason, res_id, created_ts, shopper_ref, contact_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, variant, qty, unit_price_paise, qty * unit_price_paise, reason, res_id,
                 time.time(), shopper_ref, contact_key),
            )
        return int(cur.lastrowid)

    def unnotified_demand(self, item_id: str, variant: str) -> list[dict[str, Any]]:
        """People turned away for this item who were never told it came back and
        who left something we can reach them by. Rows covered by a reservation
        are excluded - that path notifies separately."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, qty, unit_price_paise, shopper_ref, contact_key FROM demand_ledger"
                " WHERE item_id = ? AND variant = ? AND notified_ts IS NULL"
                " AND converted_ts IS NULL"
                " AND res_id IS NULL AND (shopper_ref != '' OR contact_key != '')"
                " ORDER BY created_ts", (item_id, variant)).fetchall()
        return [dict(r) for r in rows]

    def demand_already_notified(self, item_id: str, variant: str) -> int:
        """People refused this item who have already been told it is back. Not
        the same as nobody having wanted it."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) n FROM demand_ledger WHERE item_id = ? AND variant = ?"
                " AND notified_ts IS NOT NULL AND converted_ts IS NULL",
                (item_id, variant)).fetchone()
        return int(row["n"])

    def mark_demand_notified(self, row_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE demand_ledger SET notified_ts = ? WHERE id = ?",
                      (time.time(), row_id))

    def mark_demand_notified_for_reservation(self, res_id: str) -> None:
        """A reservation and its ledger row describe one refusal, so telling the
        shopper settles both."""
        with self._conn() as c:
            c.execute("UPDATE demand_ledger SET notified_ts = ? WHERE res_id = ?"
                      " AND notified_ts IS NULL", (time.time(), res_id))

    def demand_rows(self) -> list[dict[str, Any]]:
        """Refused demand, split by whether anything was done about it.

        The ledger never settled, so a refusal restocked and acted on a week
        ago still read as money currently being lost - which misled the console
        and, worse, the growth agent reasoning from it. History is kept; the
        rows now say which of three things they are, and the caller decides
        which to act on:

          outstanding  still cannot be served. This is the live problem.
          told         back in stock and the shopper was told, but they have
                       not bought yet. A second chance, not yet money.
          bought back  the shopper returned and paid. The refusal is closed
                       and this is the only row that is genuinely revenue.
          lapsed       back in stock but nobody could be told - the stock
                       problem is fixed, the sale is still gone.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT item_id, variant, reason, SUM(qty) AS lost_units,"
                " SUM(value_paise) AS lost_value_paise, COUNT(*) AS events,"
                " SUM(CASE WHEN notified_ts IS NOT NULL AND converted_ts IS NULL THEN qty"
                "   ELSE 0 END) AS notified_units,"
                " SUM(CASE WHEN notified_ts IS NOT NULL AND converted_ts IS NULL THEN value_paise"
                "   ELSE 0 END) AS notified_value_paise,"
                " SUM(CASE WHEN converted_ts IS NULL AND notified_ts IS NULL"
                "   AND (shopper_ref != '' OR contact_key != '' OR res_id IS NOT NULL)"
                "   THEN qty ELSE 0 END) AS reachable_units,"
                " SUM(CASE WHEN converted_ts IS NOT NULL THEN qty ELSE 0 END) AS bought_back_units,"
                " SUM(CASE WHEN converted_ts IS NOT NULL THEN value_paise ELSE 0 END)"
                "   AS bought_back_value_paise,"
                " GROUP_CONCAT(res_id) AS res_ids"
                " FROM demand_ledger GROUP BY item_id, variant, reason"
                " ORDER BY lost_value_paise DESC"
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["reservation_ids"] = [x for x in (d.pop("res_ids") or "").split(",") if x]
            out.append(d)
        return out

    def reservations_for(self, item_id: str, variant: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT res_id, contact_ref, status, created_ts FROM reservations"
                " WHERE item_id = ? AND variant = ? ORDER BY created_ts",
                (item_id, variant),
            ).fetchall()
        return [dict(r) for r in rows]

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

    # -- autonomous merchant agent (spec 7.5) --------------------------------
    def sales_metrics(self, days: float = 7.0) -> dict[str, Any]:
        """Real sales over the window: per-item units and revenue from PAID
        orders only. This is what the merchant agent reasons from - no views,
        no sessions, nothing we do not actually record."""
        since = time.time() - days * 86400
        per_item: dict[str, dict[str, int]] = {}
        orders = revenue = 0
        with self._conn() as c:
            rows = c.execute(
                "SELECT charge_amount, line_items FROM orders"
                " WHERE status = 'paid' AND created_ts >= ?", (since,)).fetchall()
        for r in rows:
            orders += 1
            revenue += int(r["charge_amount"])
            for li in json.loads(r["line_items"]):
                slot = per_item.setdefault(li["item_id"], {"units": 0, "revenue_paise": 0})
                slot["units"] += int(li["qty"])
                slot["revenue_paise"] += int(li["qty"]) * int(li["unit_price_paise"])
        return {"days": days, "orders": orders, "revenue_paise": revenue,
                "aov_paise": revenue // orders if orders else 0, "per_item": per_item}

    def create_proposal(self, kind: str, payload: dict[str, Any], rationale: str,
                        numbers: dict[str, Any]) -> dict[str, Any]:
        prop_id = "prop_" + uuid.uuid4().hex[:10]
        with self._conn() as c:
            c.execute(
                "INSERT INTO proposals (prop_id, kind, payload, rationale, numbers, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (prop_id, kind, json.dumps(payload), rationale, json.dumps(numbers), time.time()),
            )
        return self.proposal(prop_id)  # type: ignore[return-value]

    def proposal(self, prop_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM proposals WHERE prop_id = ?", (prop_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["numbers"] = json.loads(d["numbers"])
        return d

    def proposals(self, status: str = "") -> list[dict[str, Any]]:
        with self._conn() as c:
            if status:
                rows = c.execute("SELECT prop_id FROM proposals WHERE status = ?"
                                 " ORDER BY created_ts DESC", (status,)).fetchall()
            else:
                rows = c.execute("SELECT prop_id FROM proposals"
                                 " ORDER BY created_ts DESC").fetchall()
        return [self.proposal(r["prop_id"]) for r in rows]  # type: ignore[misc]

    def decide_proposal(self, prop_id: str, status: str, reason: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE proposals SET status = ?, decided_reason = ?, decided_ts = ?"
                      " WHERE prop_id = ? AND status = 'open'",
                      (status, reason, time.time(), prop_id))

    def add_runtime_coupon(self, coupon: dict[str, Any], days: float) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO runtime_coupons (code, coupon, expires_ts,"
                      " created_ts) VALUES (?, ?, ?, ?)",
                      (coupon["code"], json.dumps(coupon), time.time() + days * 86400,
                       time.time()))

    def runtime_coupons(self) -> list[dict[str, Any]]:
        """Live campaign coupons; expiry enforced on read, so a lapsed campaign
        simply stops applying."""
        with self._conn() as c:
            rows = c.execute("SELECT coupon FROM runtime_coupons WHERE expires_ts > ?",
                             (time.time(),)).fetchall()
        return [json.loads(r["coupon"]) for r in rows]
