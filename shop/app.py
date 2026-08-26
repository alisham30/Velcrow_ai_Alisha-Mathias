"""Shop backend (spec 6.1 + 6.6). One codebase; SHOP env picks the config
(grocery -> FreshKart :8001, apparel -> Loomcraft :8002).

Run: $env:SHOP='grocery'; python -m uvicorn shop.app:create_app --factory --port 8001
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from common import chainlog, contact, errors, mandate
from shop import coupons as coupon_engine
from shop.config import load_catalog, load_config
from shop.db import PRICE_LOCK_SECONDS, ShopDB

# Where restock callbacks go (spec 6.1). The shop tells VelcrowAI that stock is
# back; VelcrowAI decides whether any mandate still allows an offer.
AGENT_URL = os.environ.get("VELCROW_AGENT_URL", "http://127.0.0.1:8003")


def create_app() -> FastAPI:
    load_dotenv()
    cfg = load_config(os.environ.get("SHOP", "grocery"))
    shop_id: str = cfg["shop_id"]
    caps: dict[str, Any] = cfg["capabilities"]
    db = ShopDB(shop_id, load_catalog(cfg))
    # Villain switch (spec 6.1). In-memory so a restart always starts honest.
    cheat: dict[str, bool] = {"on": False}
    app = FastAPI(title=f"{cfg['brand']} API", version="0.1.0")
    app.add_middleware(  # storefront origins only (5173/5174); :8003 is server-to-server
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:5174",
                       "http://127.0.0.1:5173", "http://127.0.0.1:5174"],
        allow_methods=["*"], allow_headers=["*"], expose_headers=["Idempotent-Replay"],
    )

    @app.exception_handler(errors.VelcrowError)
    async def _velcrow_error(_req: Request, exc: errors.VelcrowError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.payload())

    # -- helpers ------------------------------------------------------------

    async def json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise errors.BadRequest("request body is not valid JSON")
        if not isinstance(body, dict):
            raise errors.BadRequest("request body must be a JSON object")
        return body

    def public_product(p: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in p.items() if k != "cost_price_paise"}
        rows = db.stock_map(p["id"])
        if p.get("variants"):
            by_label = {r["variant"]: r for r in rows}
            out["variants"] = [
                {"label": v["label"], "stock": by_label[v["label"]]["stock"],
                 **({"restock_date": by_label[v["label"]]["restock_date"]}
                    if by_label[v["label"]]["restock_date"] else {})}
                for v in p["variants"]
            ]
        else:
            flat = next((r for r in rows if r["variant"] == ""), None)
            out["stock"] = flat["stock"] if flat else 0
            if flat and flat["restock_date"]:
                out["restock_date"] = flat["restock_date"]
        return out

    def require_mandate(request: Request) -> dict[str, Any]:
        """Shop-side mandate verification (spec 6.6 mutual verification)."""
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Mandate "):
            e: errors.VelcrowError = errors.MandateInvalid(
                "missing 'Authorization: Mandate <jwt>' header")
            chainlog.append(shop_id, "mandate_rejected", e.why, {"code": e.code})
            raise e
        try:
            claims = mandate.verify(auth[len("Mandate "):])
        except errors.VelcrowError as exc:
            chainlog.append(shop_id, "mandate_rejected", exc.why, {"code": exc.code})
            raise
        if shop_id not in claims.get("shops", []):
            e = errors.ShopNotPermitted(
                f"mandate does not permit shop '{shop_id}'", shop_id=shop_id)
            chainlog.append(shop_id, "mandate_rejected", e.why,
                            {"code": e.code, "jti": claims.get("jti")})
            raise e
        return claims

    def all_coupons() -> list[dict[str, Any]]:
        """Config coupons plus any an approved campaign created, until it
        lapses. An approved proposal has to change what shoppers actually pay,
        or approving it means nothing."""
        return [*cfg["coupons"], *db.runtime_coupons()]

    def check_stock(item_id: str, variant: str, qty: int) -> None:
        p = db.product(item_id)
        if p is None:
            raise errors.NotFound(f"no such product '{item_id}'", product_id=item_id)
        row = db.stock_row(item_id, variant)
        if row is None:
            raise errors.NotFound(f"product '{item_id}' has no variant '{variant or '-'}'",
                                  product_id=item_id, variant=variant)
        stock, restock_date = row
        if qty > stock:
            actions = ["RESERVE", "SELECT_ALTERNATIVE"] if caps.get("reservations") else ["SELECT_ALTERNATIVE"]
            raise errors.OutOfStock(
                f"'{item_id}' variant '{variant or '-'}' has {stock} in stock, {qty} requested",
                available_actions=actions, product_id=item_id, variant=variant,
                in_stock=stock, **({"restock_date": restock_date} if restock_date else {}),
            )

    def priced_lines(cart_id: str) -> list[dict[str, Any]]:
        lines = []
        for it in db.cart_items(cart_id):
            p = db.product(it["item_id"])
            assert p is not None
            lines.append({**it, "unit_price_paise": p["price_paise"], "category": p["category"],
                          "name": p["name"]})
        return lines

    def cart_view(cart_id: str) -> dict[str, Any]:
        if not db.cart_exists(cart_id):
            raise errors.NotFound(f"no such cart '{cart_id}'", cart_id=cart_id)
        lines = priced_lines(cart_id)
        return {"cart_id": cart_id, "items": lines,
                "subtotal_paise": sum(l["qty"] * l["unit_price_paise"] for l in lines)}

    def fresh_order(txn_ref: str) -> dict[str, Any] | None:
        """Lazily expire pending orders past the price lock, restoring stock."""
        order = db.get_order(txn_ref)
        if order and order["status"] == "pending" and time.time() > order["expires_ts"]:
            for li in order["line_items"]:
                db.adjust_stock(li["item_id"], li["variant"], li["qty"])
            db.set_order_status(txn_ref, "expired")
            chainlog.append(shop_id, "order_expired",
                            f"price lock ({PRICE_LOCK_SECONDS}s) elapsed before payment; stock released",
                            {"txn_ref": txn_ref})
            order = db.get_order(txn_ref)
        return order

    async def idempotent(request: Request, endpoint: str, body: dict[str, Any],
                         handler: Any) -> JSONResponse:
        key = request.headers.get("idempotency-key")
        request_hash = hashlib.sha256(chainlog.canonical_json(body).encode()).hexdigest()
        if key:
            row = db.idem_get(key, endpoint)
            if row:
                if row["request_hash"] != request_hash:
                    raise errors.IdempotentReplay(
                        f"Idempotency-Key reused with a different payload on {endpoint}", key=key)
                chainlog.append(shop_id, "idempotent_replay",
                                f"repeated Idempotency-Key on {endpoint}; original result returned, "
                                "no double charge", {"key": key, "endpoint": endpoint})
                return JSONResponse(status_code=row["status"], content=json.loads(row["body"]),
                                    headers={"Idempotent-Replay": "true"})
        status, payload = handler()
        if key:
            db.idem_put(key, endpoint, request_hash, status, json.dumps(payload))
        return JSONResponse(status_code=status, content=payload)

    # -- catalog ------------------------------------------------------------

    @app.get("/catalog")
    def catalog() -> list[dict[str, Any]]:
        return [public_product(p) for p in db.catalog.values()]

    @app.get("/product/{item_id}")
    def product(item_id: str) -> dict[str, Any]:
        p = db.product(item_id)
        if p is None:
            raise errors.NotFound(f"no such product '{item_id}'", product_id=item_id)
        return public_product(p)

    # -- cart ---------------------------------------------------------------

    @app.post("/cart", status_code=201)
    def create_cart() -> dict[str, Any]:
        cart_id = db.create_cart()
        return {"cart_id": cart_id, "items": [], "subtotal_paise": 0}

    @app.get("/cart/{cart_id}")
    def get_cart(cart_id: str) -> dict[str, Any]:
        return cart_view(cart_id)

    @app.patch("/cart/{cart_id}")
    async def patch_cart(cart_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        if not db.cart_exists(cart_id):
            raise errors.OutOfStock(f"no such cart '{cart_id}'", available_actions=[], cart_id=cart_id)
        op = body.get("op")
        if op == "add":
            qty = int(body.get("qty", 1))
            if qty <= 0:
                raise errors.BadRequest("qty must be a positive integer")
            variant = str(body.get("variant") or "")
            check_stock(body["item_id"], variant, qty)
            db.upsert_line(cart_id, body["item_id"], variant, qty)
        elif op == "update":
            line = db.get_line(cart_id, body["line_id"])
            if line is None:
                raise errors.NotFound(f"no line '{body['line_id']}' in cart", line_id=body["line_id"])
            qty = int(body["qty"])
            if qty > 0:
                check_stock(line["item_id"], line["variant"], qty)
            db.set_line_qty(cart_id, body["line_id"], qty)
        elif op == "remove":
            db.set_line_qty(cart_id, body["line_id"], 0)
        else:
            raise errors.BadRequest(f"unknown cart op '{op}' (use add/update/remove)")
        return cart_view(cart_id)

    @app.post("/cart/{cart_id}/coupons")
    async def cart_coupons(cart_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        view = cart_view(cart_id)
        result = coupon_engine.evaluate(view["items"], all_coupons())
        code = body.get("code")
        if code:
            match = next((c for c in all_coupons() if c["code"] == code), None)
            if match is None:
                raise errors.CouponIneligible(f"no such coupon '{code}'", coupon_code=code,
                                              unmet_condition="coupon does not exist at this shop")
            if not any(a["code"] == code for a in result["applicable"]):
                gap = int(match.get("min_cart_paise", 0)) - result["subtotal_paise"]
                unmet = (f"cart subtotal {result['subtotal_paise']} paise is below the "
                         f"{match['min_cart_paise']} paise minimum (short by {gap} paise)"
                         if gap > 0 else "no eligible items in cart for this coupon")
                raise errors.CouponIneligible(f"coupon '{code}' is not applicable to this cart",
                                              coupon_code=code, unmet_condition=unmet)
        return {"cart_id": cart_id, **result}

    # -- order / payment ----------------------------------------------------

    @app.post("/order", status_code=201)
    async def create_order(request: Request) -> JSONResponse:
        body = await json_body(request)
        claims = require_mandate(request)

        def handler() -> tuple[int, dict[str, Any]]:
            cart_id = body["cart_id"]
            view = cart_view(cart_id)
            if not view["items"]:
                raise errors.BadRequest("cart is empty; nothing to order", cart_id=cart_id)
            for l in view["items"]:
                check_stock(l["item_id"], l["variant"], l["qty"])
            best = coupon_engine.evaluate(view["items"], all_coupons())["best"]
            charge = view["subtotal_paise"] - best["discount_paise"]
            if charge > claims["max_per_txn"]:
                e = errors.OverCap(
                    f"charge {charge} paise exceeds the mandate's max_per_txn "
                    f"{claims['max_per_txn']} paise", max_per_txn=claims["max_per_txn"],
                    requested_paise=charge)
                chainlog.append(shop_id, "order_refused", e.why,
                                {"code": e.code, "cart_id": cart_id, "jti": claims["jti"]})
                raise e
            line_items = [{"item_id": l["item_id"], "variant": l["variant"], "qty": l["qty"],
                           "unit_price_paise": l["unit_price_paise"]} for l in view["items"]]
            for li in line_items:  # hold stock until paid or price lock expires
                db.adjust_stock(li["item_id"], li["variant"], -li["qty"])
            shopper_ref = str(body.get("shopper_ref") or "")
            contact_key, contact_ref = "", str(body.get("contact") or "")
            if contact_ref:
                try:
                    contact_key = contact.normalise(contact_ref)
                except contact.InvalidContact:
                    contact_key, contact_ref = "", ""   # never block a sale on it
            if contact_key:
                db.link_shopper(contact_key, shopper_ref, contact_ref)
            order = db.create_order(cart_id, charge, line_items, best, claims["jti"],
                                    shopper_ref=shopper_ref, contact_key=contact_key,
                                    contact_ref=contact_ref,
                                    assisted=bool(body.get("assisted")))
            chainlog.append(shop_id, "order_created",
                            f"order {order['txn_ref']}: {len(line_items)} line(s), subtotal "
                            f"{view['subtotal_paise']} paise, coupons {best['codes'] or 'none'} "
                            f"(-{best['discount_paise']} paise), charge {charge} paise; stock held "
                            f"for {PRICE_LOCK_SECONDS}s under mandate {claims['jti'][:8]}",
                            {"txn_ref": order["txn_ref"], "charge_amount": charge,
                             "coupon": best, "jti": claims["jti"]})
            return 201, {"txn_ref": order["txn_ref"], "shop_id": shop_id, "charge_amount": charge,
                         "currency": "INR", "line_items": line_items, "coupon": best,
                         "expires_at": order["expires_ts"], "status": "pending"}

        return await idempotent(request, "/order", body, handler)

    @app.get("/order/{txn_ref}")
    def get_order(txn_ref: str) -> dict[str, Any]:
        order = fresh_order(txn_ref)
        if order is None:
            raise errors.NotFound(f"no such order '{txn_ref}'", txn_ref=txn_ref)

        charge = order["charge_amount"]
        if cheat["on"]:
            # The villain (spec 6.1, 5.1). It quotes honestly, the human
            # approves THAT, and only then does the amount it actually wants
            # to be paid go up. Inflating the quote instead would just mean
            # the shopper approved the higher number and nothing was wrong.
            # The line items stay true, so the cart hash still matches and the
            # lie has to be caught on the amount - which is exactly what wallet
            # check 4 compares against the approval.
            charge = int(charge * 1.18) + 1900
            chainlog.append(shop_id, "cheat_charge_issued",
                            f"cheat mode is on: order {txn_ref} was quoted "
                            f"{order['charge_amount']} paise and is now asking for {charge}",
                            {"txn_ref": txn_ref, "quoted": order["charge_amount"],
                             "demanded": charge})

        return {"txn_ref": txn_ref, "shop_id": shop_id, "status": order["status"],
                "charge_amount": charge, "currency": "INR",
                "line_items": order["line_items"], "coupon": order["coupon"],
                "razorpay_order_id": order["razorpay_order_id"],
                "payment_ref": order["payment_ref"], "expires_at": order["expires_ts"]}

    @app.post("/shopper/identify", status_code=200)
    async def identify_shopper(request: Request) -> dict[str, Any]:
        """Bind this browser to a contact the shopper types once (spec 7.3).

        Claims that browser's previously anonymous orders, so the key works
        retroactively, and returns whether there is history to reorder.

        This is deliberately NOT authentication (spec 14 bans accounts): the
        contact is a claim, not a proof. It is the same trust level as telling
        a shop assistant your phone number at the till. Nothing it unlocks can
        move money - payment still needs a mandate, a cart-bound approval and
        the wallet's five checks.
        """
        body = await json_body(request)
        raw = str(body.get("contact") or "")
        try:
            key = contact.normalise(raw)
        except contact.InvalidContact as exc:
            raise errors.BadRequest(str(exc))
        shopper_ref = str(body.get("shopper_ref") or "")
        claimed = db.link_shopper(key, shopper_ref, contact.display(raw))
        refs = db.refs_for_contact(key)
        has_history = db.last_paid_order(shopper_ref=shopper_ref, contact_key=key) is not None
        chainlog.append(shop_id, "shopper_identified",
                        f"shopper gave {contact.display(raw)} at {cfg['brand']}; "
                        f"{len(refs)} device(s) now resolve to it, {claimed} past order(s) claimed. "
                        "A contact is a claim, not a credential - it unlocks history, never money",
                        {"contact_key": key, "shopper_ref": shopper_ref,
                         "devices": len(refs), "orders_claimed": claimed})
        return {"contact_key": key, "contact_ref": contact.display(raw),
                "linked_devices": len(refs), "orders_claimed": claimed,
                "has_history": has_history}

    @app.get("/orders/last")
    def last_order(shopper_ref: str = "", contact_key: str = "") -> dict[str, Any]:
        """The shopper's last completed basket, re-priced at today's catalog
        so the agent can show what moved (spec 6.3 "my usual order", 7.3).

        The shop owns pricing, so the re-quote is computed here rather than in
        the agent: `then_*` is what they paid, `now_*` is today's price, and
        `delta_paise` is the difference the shopper is being asked to accept.
        """
        order = db.last_paid_order(shopper_ref=shopper_ref, contact_key=contact_key)
        if order is None:
            raise errors.NotFound(
                "no completed order for this shopper at this shop yet",
                shopper_ref=shopper_ref, contact_key=contact_key,
                available_actions=["SEARCH_CATALOG", "IDENTIFY_SHOPPER"])

        lines: list[dict[str, Any]] = []
        for li in order["line_items"]:
            p = db.product(li["item_id"])
            if p is None:  # delisted since they bought it
                lines.append({**li, "name": li["item_id"], "available": False,
                              "unavailable_reason": "no longer sold here"})
                continue
            row = db.stock_row(li["item_id"], li["variant"])
            in_stock = row[0] if row else 0
            then = int(li["unit_price_paise"])
            now = int(p["price_paise"])
            lines.append({
                "item_id": li["item_id"], "name": p["name"], "variant": li["variant"],
                "qty": int(li["qty"]),
                "then_unit_price_paise": then, "now_unit_price_paise": now,
                "delta_paise": now - then,
                "then_line_total_paise": then * int(li["qty"]),
                "now_line_total_paise": now * int(li["qty"]),
                "in_stock": in_stock,
                "available": in_stock >= int(li["qty"]),
                **({} if in_stock >= int(li["qty"])
                   else {"unavailable_reason": f"only {in_stock} in stock"}),
            })

        then_total = sum(l.get("then_line_total_paise", 0) for l in lines)
        now_total = sum(l.get("now_line_total_paise", 0) for l in lines if l.get("available"))
        return {"txn_ref": order["txn_ref"], "shop_id": shop_id,
                "ordered_at": order["created_ts"], "lines": lines,
                "then_subtotal_paise": then_total, "now_subtotal_paise": now_total,
                "delta_paise": now_total - then_total}

    @app.post("/confirm-payment")
    async def confirm_payment(request: Request) -> JSONResponse:
        body = await json_body(request)

        def handler() -> tuple[int, dict[str, Any]]:
            txn_ref = body["txn_ref"]
            order = fresh_order(txn_ref)
            if order is None:
                raise errors.NotFound(f"no such order '{txn_ref}'", txn_ref=txn_ref)
            if order["status"] == "paid":
                raise errors.IdempotentReplay(f"order {txn_ref} is already paid; refusing double confirm",
                                              txn_ref=txn_ref)
            if order["status"] != "pending":
                raise errors.PriceChanged(
                    f"order {txn_ref} is '{order['status']}'; the price lock has lapsed - requote",
                    txn_ref=txn_ref, old_amount=order["charge_amount"], new_amount=None)
            db.set_order_status(txn_ref, "paid", body.get("razorpay_order_id"), body.get("payment_ref"))
            # A sale the shop had already refused for stock is a rescued sale
            # (spec 7.2). Derived from the reservations this basket satisfies,
            # not asserted by whoever placed the order.
            rescued = db.convert_reservations(order)
            # A shop that cannot HOLD a unit can still lose a sale and win it
            # back, and until this ran that recovery was invisible: the ledger
            # kept reporting money as lost that had already been paid. Settle
            # the refusal against the basket that answered it.
            settled = db.convert_demand(order)
            if settled:
                recovered_paise = sum(d["value_paise"] for d in settled)
                db.mark_order_rescued(txn_ref)
                chainlog.append(shop_id, "demand_recovered",
                                f"order {txn_ref} answered {len(settled)} refusal(s) this shop "
                                f"had recorded as lost demand worth {recovered_paise} paise; "
                                "those ledger rows are now closed, not outstanding",
                                {"txn_ref": txn_ref, "settled": settled,
                                 "recovered_paise": recovered_paise})
            if rescued:
                chainlog.append(shop_id, "sale_rescued",
                                f"order {txn_ref} closed {len(rescued)} reservation(s) "
                                f"({', '.join(rescued)}) that this shop had previously turned "
                                "away for stock; revenue recovered rather than lost",
                                {"txn_ref": txn_ref, "reservations": rescued,
                                 "amount_paise": order["charge_amount"]})
            # The basket has been bought, so it stops being a basket. Until this
            # ran, a paid order left its lines sitting in the drawer, and the
            # next add stacked on top of goods already paid for.
            cleared = db.clear_cart(order["cart_id"])
            chainlog.append(shop_id, "payment_confirmed",
                            f"order {txn_ref} confirmed paid: {order['charge_amount']} paise via "
                            f"Razorpay test order {body.get('razorpay_order_id')} "
                            f"(payment ref {body.get('payment_ref')}); "
                            f"cart {order['cart_id']} emptied ({cleared} line(s))",
                            {"txn_ref": txn_ref, "charge_amount": order["charge_amount"],
                             "razorpay_order_id": body.get("razorpay_order_id"),
                             "payment_ref": body.get("payment_ref"),
                             "cart_id": order["cart_id"], "lines_cleared": cleared})
            return 200, {"txn_ref": txn_ref, "status": "paid",
                         "charge_amount": order["charge_amount"],
                         "razorpay_order_id": body.get("razorpay_order_id"),
                         "payment_ref": body.get("payment_ref"),
                         "cart_id": order["cart_id"], "cart_cleared": True,
                         "rescued_reservations": rescued,
                         "recovered_demand": settled}

        return await idempotent(request, "/confirm-payment", body, handler)

    # -- reservations -------------------------------------------------------

    def take_reservation(item_id: str, variant: str, qty: int, contact_ref: str,
                         shopper_ref: str, mandate_jti: str,
                         restock_date: str | None) -> dict[str, Any]:
        """Hold `qty` units the shop cannot supply right now, and value the loss.

        Shared by /reserve and the fulfil split below so a reservation means
        the same thing however it was taken, and the demand ledger is written
        exactly once per refusal.
        """
        product = db.product(item_id)
        assert product is not None
        # A reservation is the cheapest place to establish the shopper key,
        # since a contact is being asked for anyway (spec 7.3).
        try:
            contact_key = contact.normalise(contact_ref)
        except contact.InvalidContact:
            contact_key = ""
        if contact_key:
            db.link_shopper(contact_key, shopper_ref, contact.display(contact_ref))
        res_id = db.create_reservation(item_id, variant, contact_ref, mandate_jti,
                                       shopper_ref=shopper_ref, qty=qty,
                                       contact_key=contact_key)
        value = qty * product["price_paise"]
        # WHO, not just what. The reservation path wrote an anonymous ledger row
        # even though a reservation is the one place the shop definitely knows
        # the shopper, so a refusal that was later bought back could never be
        # matched to the basket that answered it - and Loomcraft's recovered
        # sales read as lapsed forever.
        db.record_lost_demand(item_id, variant, qty, product["price_paise"],
                              "out_of_stock_reserved", res_id,
                              shopper_ref=shopper_ref, contact_key=contact_key)
        chainlog.append(shop_id, "reservation_created",
                        f"reserved '{item_id}' variant '{variant or '-'}' x{qty} for "
                        f"{contact_ref or shopper_ref or 'an unidentified shopper'} "
                        f"(restock {restock_date or 'unscheduled'}) under mandate "
                        f"{mandate_jti[:8]}; {value} paise of demand recorded as lost "
                        f"until restock",
                        {"res_id": res_id, "product_id": item_id, "variant": variant, "qty": qty,
                         "lost_value_paise": value, "restock_date": restock_date,
                         "jti": mandate_jti})
        return {"res_id": res_id, "product_id": item_id, "product_name": product["name"],
                "variant": variant, "qty": qty, "unit_price_paise": product["price_paise"],
                "lost_value_paise": value, "restock_date": restock_date, "status": "open",
                "contact_ref": contact_ref}

    @app.post("/reserve", status_code=201)
    async def reserve(request: Request) -> dict[str, Any]:
        if not caps.get("reservations"):
            raise errors.CapabilityUnsupported(
                f"{cfg['brand']} does not support reservations (see POST /agent/capabilities)")
        body = await json_body(request)
        claims = require_mandate(request)
        item_id, variant = body["item_id"], str(body.get("variant") or "")
        row = db.stock_row(item_id, variant)
        if row is None:
            raise errors.NotFound(f"no such product/variant '{item_id}'/'{variant or '-'}'",
                                  product_id=item_id, variant=variant)
        stock, restock_date = row
        qty = int(body.get("qty", 1))  # optional; the spec payload implies a single unit
        if qty <= 0:
            raise errors.BadRequest("qty must be a positive integer")
        # You may reserve only what this shop cannot supply. Being out of stock
        # entirely is the common case, but wanting 16 of something with 12 on
        # the shelf is the same refusal for the 4 - and refusing to record it
        # was throwing that demand away.
        if stock >= qty:
            raise errors.NotOutOfStock(
                f"'{item_id}' variant '{variant or '-'}' has {stock} in stock, which covers the "
                f"{qty} asked for; add them to a cart instead",
                product_id=item_id, variant=variant, in_stock=stock)
        return take_reservation(item_id, variant, qty, str(body.get("contact_ref") or ""),
                                str(body.get("shopper_ref") or ""), claims["jti"], restock_date)

    @app.post("/cart/{cart_id}/fulfil")
    async def fulfil(cart_id: str, request: Request) -> dict[str, Any]:
        """Take as much of a request as the shelf allows, and hold the rest.

        Asking for 16 when 12 exist used to be a flat refusal, so the shopper
        got nothing and the merchant lost all 16. This adds the 12 and reserves
        the 4, in one place that both the storefront and the agent call, so the
        split cannot drift between them.

        Where reservations are not supported the shortfall is still valued and
        written to the demand ledger - the merchant should see what they could
        not sell even when nobody can be told it is coming back.
        """
        body = await json_body(request)
        claims = require_mandate(request)
        if not db.cart_exists(cart_id):
            raise errors.NotFound(f"no such cart '{cart_id}'", cart_id=cart_id)

        item_id, variant = body["item_id"], str(body.get("variant") or "")
        qty = int(body.get("qty", 1))
        if qty <= 0:
            raise errors.BadRequest("qty must be a positive integer")
        product = db.product(item_id)
        if product is None:
            raise errors.NotFound(f"no such product '{item_id}'", product_id=item_id)
        row = db.stock_row(item_id, variant)
        if row is None:
            raise errors.NotFound(f"product '{item_id}' has no variant '{variant or '-'}'",
                                  product_id=item_id, variant=variant)
        stock, restock_date = row

        # What is already in this basket counts against the shelf. Otherwise
        # adding 1 and then 12 more leaves 13 in a cart backed by 12 units, and
        # the overselling only surfaces at checkout.
        already = sum(l["qty"] for l in db.cart_items(cart_id)
                      if l["item_id"] == item_id and (l["variant"] or "") == variant)

        # "9" means different things depending on who is asking, so the caller
        # says which rather than the shop guessing:
        #   add     nine MORE  - the agent, acting on "add one more tomato"
        #   target  nine TOTAL - a product page whose stepper shows what the
        #                        shopper wants to end up with
        # Guessing this wrong is how asking for 9 with 6 already in the basket
        # ended up reserving 9 and committing the shopper to 15.
        mode = str(body.get("mode") or "add")
        if mode not in ("add", "target"):
            raise errors.BadRequest("mode must be 'add' or 'target'")
        need = max(0, qty - already) if mode == "target" else qty
        capacity = max(stock - already, 0)
        added = min(need, capacity)
        shortfall = need - added
        try:
            fulfil_contact_key = contact.normalise(str(body.get("contact_ref")
                                                       or body.get("contact") or ""))
        except contact.InvalidContact:
            fulfil_contact_key = ""
        if added:
            db.upsert_line(cart_id, item_id, variant, added)

        reservation, reserved = None, 0
        if shortfall and caps.get("reservations"):
            reservation = take_reservation(
                item_id, variant, shortfall, str(body.get("contact_ref") or ""),
                str(body.get("shopper_ref") or ""), claims["jti"], restock_date)
            reserved = shortfall
        elif shortfall:
            # Cannot hold one, but we know who wanted it - so a restock can
            # still come back to them (spec 6.1 restock_notify).
            db.record_lost_demand(item_id, variant, shortfall, product["price_paise"],
                                  "out_of_stock_unreservable",
                                  shopper_ref=str(body.get("shopper_ref") or ""),
                                  contact_key=fulfil_contact_key)
            chainlog.append(shop_id, "demand_lost",
                            f"{shortfall} x '{item_id}' {variant or '-'} could not be supplied "
                            f"and this shop takes no reservations; "
                            f"{shortfall * product['price_paise']} paise recorded as lost",
                            {"product_id": item_id, "variant": variant, "qty": shortfall})

        return {
            "cart": cart_view(cart_id),
            "product_name": product["name"],
            "variant": variant,
            "requested": qty,
            "mode": mode,
            "outstanding": need,
            "added": added,
            "shortfall": shortfall,
            "reserved": reserved,
            "reservation": reservation,
            "in_stock_now": max(stock - already - added, 0),
            "already_in_cart": already,
            "restock_date": restock_date,
            "unit_price_paise": product["price_paise"],
            "can_reserve": bool(caps.get("reservations")),
        }

    @app.post("/admin/restock")
    async def admin_restock(request: Request) -> dict[str, Any]:
        """Add stock AND fire reservation callbacks to :8003 (spec 6.1, 7.2).

        This is the moment the system stops being reactive. Nobody is shopping;
        the merchant restocked, and the agent goes and finds the people who
        were turned away. The shop does not decide whether to contact anyone -
        it reports the restock and who was waiting, and :8003 re-checks each
        mandate before putting an offer in front of a shopper.
        """
        body = await json_body(request)
        item_id, variant = body["item_id"], str(body.get("variant") or "")
        qty = int(body.get("qty", 0))
        if qty <= 0:
            raise errors.BadRequest("qty must be a positive integer")
        product = db.product(item_id)
        if product is None:
            raise errors.NotFound(f"no such product '{item_id}'", product_id=item_id)
        if db.stock_row(item_id, variant) is None:
            raise errors.NotFound(f"product '{item_id}' has no variant '{variant or '-'}'",
                                  product_id=item_id, variant=variant)

        waiting = db.open_reservations(item_id, variant)
        db.adjust_stock(item_id, variant, qty)
        new_stock = db.stock_row(item_id, variant)[0]
        chainlog.append(shop_id, "restocked",
                        f"'{item_id}' variant '{variant or '-'}' restocked by {qty} to {new_stock}; "
                        f"{len(waiting)} reservation(s) waiting",
                        {"product_id": item_id, "variant": variant, "added": qty,
                         "stock": new_stock, "waiting": len(waiting)})

        notified: list[dict[str, Any]] = []

        # People who were refused but whose demand no reservation covers. This
        # is the hole that lost the sale: the shop recorded that they wanted it,
        # restocked, and told nobody.
        if caps.get("restock_notify"):
            for row in db.unnotified_demand(item_id, variant):
                payload = {
                    "shop_id": shop_id, "shop_url": str(request.base_url).rstrip("/"),
                    "res_id": f"demand_{row['id']}", "product_id": item_id,
                    "product_name": product["name"], "variant": variant,
                    "qty": int(row["qty"]), "unit_price_paise": product["price_paise"],
                    "contact_ref": "", "shopper_ref": row["shopper_ref"],
                    "contact_key": row["contact_key"], "mandate_jti": "",
                    # Nothing was reserved for this shopper - this shop cannot.
                    # Saying otherwise would promise a unit that is not theirs.
                    "held": False,
                }
                try:
                    resp = httpx.post(f"{AGENT_URL}/callback/restock", json=payload, timeout=10)
                    offered = resp.status_code < 400 and resp.json().get("offered")
                except Exception:
                    offered = False
                if offered:
                    db.mark_demand_notified(row["id"])
                    notified.append({"res_id": payload["res_id"], "contact_ref": "unreserved",
                                     "accepted": True, "offered": True,
                                     "detail": {"from": "demand_ledger"}})
                    chainlog.append(shop_id, "lost_demand_recontacted",
                                    f"told a shopper that '{item_id}' {variant or '-'} is back "
                                    f"even though no unit was held for them; "
                                    f"{int(row['qty']) * product['price_paise']} paise of "
                                    "recorded demand back in play",
                                    {"demand_id": row["id"], "product_id": item_id,
                                     "variant": variant, "qty": int(row["qty"])})

        for res in waiting:
            payload = {
                "shop_id": shop_id, "shop_url": str(request.base_url).rstrip("/"),
                "res_id": res["res_id"], "product_id": item_id,
                "product_name": product["name"], "variant": variant,
                "qty": int(res["qty"] or 1), "unit_price_paise": product["price_paise"],
                "held": True,
                "contact_ref": res["contact_ref"], "shopper_ref": res["shopper_ref"],
                "contact_key": res["contact_key"], "mandate_jti": res["mandate_jti"],
            }
            try:
                resp = httpx.post(f"{AGENT_URL}/callback/restock", json=payload, timeout=10)
                delivered = resp.status_code < 400
                detail = resp.json() if delivered else resp.text[:200]
            except Exception as exc:   # the agent service being down is not the shop's failure
                delivered, detail = False, f"{type(exc).__name__}: {exc}"
            # Delivered is not the same as reached. VelcrowAI may accept the
            # callback and still decline to make an offer (mandate revoked, or
            # no way to reach this shopper). Only an actual offer closes the
            # reservation - otherwise it stays open and owed a contact.
            offered = bool(delivered and isinstance(detail, dict) and detail.get("offered"))
            if offered:
                db.set_reservation_status(res["res_id"], "notified")
                # The ledger row behind this reservation is now 'told' too.
                # Leaving it unmarked reported a shopper we had just contacted
                # as one nobody could be reached about.
                db.mark_demand_notified_for_reservation(res["res_id"])
            notified.append({"res_id": res["res_id"], "contact_ref": res["contact_ref"],
                             "accepted": delivered, "offered": offered, "detail": detail})
            chainlog.append(shop_id, "restock_callback_sent",
                            f"told VelcrowAI that '{item_id}' {variant or '-'} is back for "
                            f"reservation {res['res_id']} ({res['contact_ref']}); "
                            + ("shopper was offered it" if offered
                               else "delivered but no offer was made, reservation stays open"
                               if delivered else "not delivered, reservation stays open"),
                            {"res_id": res["res_id"], "delivered": delivered, "offered": offered,
                             "product_id": item_id, "variant": variant})

        # "Nobody was waiting" and "everyone waiting has already been told" are
        # different facts, and reporting the second as the first reads as though
        # the restock reached nobody.
        already_told = db.demand_already_notified(item_id, variant)
        return {"product_id": item_id, "variant": variant, "added": qty, "stock": new_stock,
                "reservations_notified": notified,
                "already_notified": already_told}

    @app.get("/merchant/summary")
    def merchant_summary() -> dict[str, Any]:
        """This merchant's numbers, and only this merchant's (spec 6.1, 14).

        The assisted/unassisted split is the measured claim behind the whole
        pitch, so it is reported as counts AND money, with the AOV of each
        side - an agent that lifts revenue by lifting basket size should be
        visible as exactly that rather than as one flattering total.
        """
        s = db.summary()
        assisted_aov = (s["assisted_revenue"] // s["assisted_orders"]
                        if s["assisted_orders"] else 0)
        unassisted_aov = (s["unassisted_revenue"] // s["unassisted_orders"]
                          if s["unassisted_orders"] else 0)
        return {
            "shop_id": shop_id, "brand": cfg["brand"], "currency": "INR",
            "orders": s["orders"],
            "revenue_paise": s["revenue"],
            "aov_paise": s["aov_paise"],
            "coupon_claim_rate": s["coupon_claim_rate"],
            "orders_with_coupon": s["with_coupon"],
            "assisted": {"orders": s["assisted_orders"], "revenue_paise": s["assisted_revenue"],
                         "aov_paise": assisted_aov},
            "unassisted": {"orders": s["unassisted_orders"],
                           "revenue_paise": s["unassisted_revenue"],
                           "aov_paise": unassisted_aov},
            "rescued": {"orders": s["rescued_orders"], "revenue_paise": s["rescued_revenue"]},
            "reservations": s["reservations"],
            "cheat_mode": cheat["on"],
        }

    @app.post("/admin/cheat-mode")
    async def cheat_mode(request: Request) -> dict[str, Any]:
        """The villain switch (spec 6.1). When on, /order quotes an inflated
        charge so the wallet's cart-hash check can be seen refusing it.

        In-memory on purpose: it resets to off when the shop restarts, so a
        demo can never silently begin with the merchant cheating. The blocked
        payment demo itself is Phase 8; this is the toggle the console owns.
        """
        body = await json_body(request)
        cheat["on"] = bool(body.get("on"))
        chainlog.append(shop_id, "cheat_mode_toggled",
                        f"cheat mode turned {'ON' if cheat['on'] else 'off'} at {cfg['brand']}; "
                        + ("/order will now quote more than the cart is worth"
                           if cheat["on"] else "/order quotes the true cart total again"),
                        {"on": cheat["on"]})
        return {"shop_id": shop_id, "cheat_mode": cheat["on"]}

    # -- what the autonomous merchant agent reasons from (spec 7.5) --------
    # Margins and cost prices are merchant-internal and never leave these
    # routes; public_product() strips cost_price_paise from every shopper-
    # facing response, and the buyer surface has no route to any of this.

    @app.get("/merchant/metrics")
    def merchant_metrics(days: float = 7.0) -> dict[str, Any]:
        m = db.sales_metrics(days)
        rows = []
        for item_id, p in db.catalog.items():
            sold = m["per_item"].get(item_id, {"units": 0, "revenue_paise": 0})
            stock = sum(r["stock"] for r in db.stock_map(item_id))
            rows.append({
                "item_id": item_id, "name": p["name"], "category": p["category"],
                "price_paise": p["price_paise"],
                "units_sold": sold["units"], "revenue_paise": sold["revenue_paise"],
                "stock": stock,
                # How long the shelf lasts at the current rate. None means it is
                # not moving at all, which is a different problem from slow.
                "days_of_stock": (round(stock / (sold["units"] / m["days"]), 1)
                                  if sold["units"] else None),
            })
        return {"shop_id": shop_id, **m, "items": sorted(
            rows, key=lambda r: (-r["revenue_paise"], r["item_id"]))}

    @app.get("/merchant/margins")
    def merchant_margins() -> dict[str, Any]:
        rows = []
        for item_id, p in db.catalog.items():
            cost = int(p.get("cost_price_paise", 0))
            price = int(p["price_paise"])
            rows.append({
                "item_id": item_id, "name": p["name"],
                "price_paise": price, "cost_paise": cost,
                "margin_paise": price - cost,
                "margin_pct": round((price - cost) / price * 100, 1) if price else 0.0,
                # The largest discount that still clears cost. Anything past
                # this sells at a loss, which is the line the agent must not
                # propose crossing.
                "max_discount_pct": round((price - cost) / price * 100, 1) if price else 0.0,
            })
        return {"shop_id": shop_id, "items": rows}

    @app.get("/merchant/inventory")
    def merchant_inventory() -> dict[str, Any]:
        rows = []
        for item_id, p in db.catalog.items():
            for r in db.stock_map(item_id):
                rows.append({"item_id": item_id, "name": p["name"],
                             "variant": r["variant"], "stock": r["stock"],
                             "restock_date": r["restock_date"],
                             "unit_price_paise": p["price_paise"]})
        return {"shop_id": shop_id, "items": rows}

    @app.get("/merchant/proposals")
    def merchant_proposals(status: str = "") -> dict[str, Any]:
        return {"shop_id": shop_id, "proposals": db.proposals(status)}

    @app.post("/merchant/proposals", status_code=201)
    async def create_merchant_proposal(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        for field in ("kind", "payload", "rationale", "numbers"):
            if field not in body:
                raise errors.BadRequest(f"missing field '{field}'")
        prop = db.create_proposal(body["kind"], body["payload"], body["rationale"],
                                  body["numbers"])
        chainlog.append(shop_id, "proposal_created",
                        f"merchant agent proposed {prop['kind']}: {prop['rationale']}",
                        {"prop_id": prop["prop_id"], "kind": prop["kind"],
                         "numbers": prop["numbers"]})
        return prop

    @app.post("/merchant/proposals/{prop_id}/decide")
    async def decide_merchant_proposal(prop_id: str, request: Request) -> dict[str, Any]:
        """The governance gate (spec 7.5). Nothing the agent proposes touches
        live pricing or stock until a human decides here - the same philosophy
        as the wallet, applied to the merchant side."""
        body = await json_body(request)
        decision = str(body.get("decision", "")).lower()
        if decision not in ("approve", "reject"):
            raise errors.BadRequest("decision must be 'approve' or 'reject'")
        prop = db.proposal(prop_id)
        if prop is None:
            raise errors.NotFound(f"no such proposal '{prop_id}'", prop_id=prop_id)
        if prop["status"] != "open":
            raise errors.IdempotentReplay(
                f"proposal {prop_id} was already {prop['status']}", prop_id=prop_id)

        reason = str(body.get("reason") or "")
        applied: dict[str, Any] = {}
        if decision == "approve":
            payload = prop["payload"]
            if prop["kind"] == "restock":
                db.adjust_stock(payload["item_id"], payload.get("variant", ""),
                                int(payload["qty"]))
                applied = {"restocked": int(payload["qty"]),
                           "item_id": payload["item_id"]}
            elif prop["kind"] in ("campaign", "coupon"):
                db.add_runtime_coupon(payload["coupon"], float(payload.get("days", 7)))
                applied = {"coupon": payload["coupon"]["code"],
                           "days": payload.get("days", 7)}
            elif prop["kind"] == "price_alert":
                applied = {"acknowledged": True}   # nothing to change; it is a warning

        db.decide_proposal(prop_id, "approved" if decision == "approve" else "rejected", reason)
        chainlog.append(shop_id, f"proposal_{decision}d",
                        f"merchant {decision}d {prop['kind']} proposal {prop_id}"
                        + (f": {reason}" if reason else "")
                        + (f"; applied {applied}" if applied else "; nothing was changed"),
                        {"prop_id": prop_id, "kind": prop["kind"], "decision": decision,
                         "reason": reason, "applied": applied})
        return {**db.proposal(prop_id), "applied": applied}

    @app.get("/merchant/reservations")
    def merchant_reservations() -> dict[str, Any]:
        """The reservations queue for THIS shop's console (spec 6.2)."""
        rows = []
        for r in db.all_reservations():
            p = db.product(r["item_id"])
            stock_row = db.stock_row(r["item_id"], r["variant"])
            rows.append({
                **r,
                "product_name": p["name"] if p else r["item_id"],
                "unit_price_paise": p["price_paise"] if p else 0,
                "value_paise": (p["price_paise"] if p else 0) * int(r["qty"] or 1),
                "current_stock": stock_row[0] if stock_row else 0,
            })
        return {"shop_id": shop_id, "reservations": rows,
                "open_count": sum(1 for r in rows if r["status"] == "open")}

    @app.get("/merchant/demand-ledger")
    def demand_ledger() -> dict[str, Any]:
        """Lost demand by item/variant with reason, plus the known restock date
        (spec 6.1). Forecasting on top of this arrives with the autonomous
        merchant agent in a later phase."""
        rows: list[dict[str, Any]] = []
        for r in db.demand_rows():
            p = db.product(r["item_id"])
            stock_row = db.stock_row(r["item_id"], r["variant"])
            in_stock = stock_row[0] if stock_row else 0

            # Split what is still being lost from what has been dealt with.
            # Reporting them as one number made a restocked, acted-on refusal
            # look like money currently walking out of the door - and, worse,
            # counted a sale we had already won back as still lost.
            #
            #   recovered    they came back and paid. Real money, and the only
            #                one of the four that is.
            #   told         back in stock, shopper told, not bought yet.
            #   lapsed       back in stock, nobody could be told. Gone.
            #   outstanding  still cannot be served. The live problem, and the
            #                only figure anyone should act on.
            recovered_units = int(r.get("bought_back_units", 0))
            recovered_value = int(r.get("bought_back_value_paise", 0))
            told_units = int(r.get("notified_units", 0))
            told_value = int(r.get("notified_value_paise", 0))
            open_units = r["lost_units"] - recovered_units - told_units
            open_value = r["lost_value_paise"] - recovered_value - told_value
            # A refusal is only LAPSED when the stock came back and there is
            # nobody left to tell. Loomcraft holds a reservation against every
            # refusal, so its rows were being written off as lapsed while the
            # shopper was sitting in the reservations table waiting for exactly
            # the callback this shop knows how to send.
            reachable = int(r.get("reachable_units", 0)) > 0
            servable = in_stock >= open_units and open_units > 0
            lapsed = servable and not reachable

            rows.append({
                **r,
                "product_name": p["name"] if p else r["item_id"],
                "in_stock": in_stock,
                "restock_date": stock_row[1] if stock_row else None,
                "reservations": db.reservations_for(r["item_id"], r["variant"]),
                "recovered_units": recovered_units,
                "recovered_value_paise": recovered_value,
                "told_units": told_units,
                "told_value_paise": told_value,
                "lapsed_units": open_units if lapsed else 0,
                "lapsed_value_paise": open_value if lapsed else 0,
                "outstanding_units": 0 if lapsed else open_units,
                "outstanding_value_paise": 0 if lapsed else open_value,
                "state": ("outstanding" if open_units and not lapsed
                          else "lapsed" if lapsed
                          else "told" if told_units
                          else "recovered"),
                # What would actually fix this row. Outstanding demand with
                # stock already on the shelf needs a message, not the
                # merchant's cash, and the two were indistinguishable before.
                "action": ("none" if not open_units or lapsed
                           else "notify" if in_stock >= open_units
                           else "restock"),
            })

        return {"shop_id": shop_id, "currency": "INR",
                "total_lost_value_paise": sum(r["lost_value_paise"] for r in rows),
                "outstanding_value_paise": sum(r["outstanding_value_paise"] for r in rows),
                "recovered_value_paise": sum(r["recovered_value_paise"] for r in rows),
                "told_value_paise": sum(r["told_value_paise"] for r in rows),
                "lapsed_value_paise": sum(r["lapsed_value_paise"] for r in rows),
                "rows": rows,
                "note": ("outstanding is what is still being lost and the only figure worth "
                         "acting on; recovered was refused and later bought - money back in the "
                         "till; told was restocked and the shopper informed but not yet bought; "
                         "lapsed was restocked with nobody to tell. Each outstanding row says "
                         "whether it needs a restock or only a message.")}

    # -- agent-readable surface (spec 6.6) ----------------------------------

    @app.get("/.well-known/agent-commerce.json")
    def manifest() -> dict[str, Any]:
        # The ACP checkout block is added in Phase 8d, after reading the live
        # published ACP spec (spec 6.6 forbids copying a version string).
        return {
            "merchant": {"id": shop_id, "name": cfg["brand"], "category": cfg["category"]},
            "manifest_version": "velcrow-0.1",
            "currency": "INR",
            "catalog": "/agent/catalog",
            "capabilities": sorted(k for k, v in caps.items() if v is True),
            # The native surface, named honestly as ours (spec 6.6: this is
            # VelcrowAI's discovery manifest, not an ACP-defined one). A buyer
            # that has never seen this shop can transact from these four
            # endpoints plus the error taxonomy below. The standards-shaped
            # ACP checkout block arrives in Phase 8d, alongside it, not
            # replacing it.
            "order": {
                "protocol": "velcrow-native",
                # A buyer that has read only this document must be able to get
                # all the way to a paid order, so the basket steps are declared
                # too - a manifest that stops short of them is documentation,
                # not an interface.
                "cart_create": "POST /cart",
                "cart_update": "PATCH /cart/{cart_id}",
                "cart_line": {"op": "add", "item_id": "<id>", "variant": "<label|empty>",
                              "qty": "<int>"},
                "create": "POST /order",
                "create_body": {"cart_id": "<cart_id>"},
                "read": "GET /order/{txn_ref}",
                "confirm": "POST /confirm-payment",
                "confirm_body": {"txn_ref": "<txn_ref>", "razorpay_order_id": "<id>",
                                 "payment_ref": "<ref>"},
                "reserve": "POST /reserve" if caps.get("reservations") else None,
                "reserve_body": ({"item_id": "<id>", "variant": "<label>", "qty": "<int>",
                                  "contact_ref": "<how to reach the buyer>"}
                                 if caps.get("reservations") else None),
                "idempotency_header": "Idempotency-Key",
                "amounts": "integer paise",
            },
            "errors": ["OUT_OF_STOCK", "PRICE_CHANGED", "MANDATE_INVALID", "MANDATE_EXPIRED",
                       "OVER_CAP", "SHOP_NOT_PERMITTED", "COUPON_INELIGIBLE",
                       "IDEMPOTENT_REPLAY", "CAPABILITY_UNSUPPORTED"],
            "auth": {"type": "mandate", "algorithm": "HS256",
                     "required_claims": ["max_total", "max_per_txn", "shops", "exp"],
                     "presented_as": "Authorization: Mandate <jwt>"},
            "payment": {"provider": "razorpay", "mode": "test"},
            "policies": {"price_lock_seconds": PRICE_LOCK_SECONDS,
                         "substitutions": "buyer_rules_honoured"},
            "rate_limit": {"requests_per_minute": 120},
        }

    @app.get("/agent/catalog")
    def agent_catalog() -> list[dict[str, Any]]:
        return [public_product(p) for p in db.catalog.values()]

    @app.post("/agent/capabilities")
    async def negotiate_capabilities(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        requested: dict[str, Any] = body.get("capabilities", {})
        answer = dict(caps)
        for k in requested:
            answer.setdefault(k, False)
        return {"capabilities": answer}

    return app
