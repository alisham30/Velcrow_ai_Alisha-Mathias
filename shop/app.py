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

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import chainlog, errors, mandate
from shop import coupons as coupon_engine
from shop.config import load_catalog, load_config
from shop.db import PRICE_LOCK_SECONDS, ShopDB


def create_app() -> FastAPI:
    load_dotenv()
    cfg = load_config(os.environ.get("SHOP", "grocery"))
    shop_id: str = cfg["shop_id"]
    caps: dict[str, Any] = cfg["capabilities"]
    db = ShopDB(shop_id, load_catalog(cfg))
    app = FastAPI(title=f"{cfg['brand']} API", version="0.1.0")

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
        result = coupon_engine.evaluate(view["items"], cfg["coupons"])
        code = body.get("code")
        if code:
            match = next((c for c in cfg["coupons"] if c["code"] == code), None)
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
            best = coupon_engine.evaluate(view["items"], cfg["coupons"])["best"]
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
            order = db.create_order(cart_id, charge, line_items, best, claims["jti"])
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
        return {"txn_ref": txn_ref, "shop_id": shop_id, "status": order["status"],
                "charge_amount": order["charge_amount"], "currency": "INR",
                "line_items": order["line_items"], "coupon": order["coupon"],
                "razorpay_order_id": order["razorpay_order_id"],
                "payment_ref": order["payment_ref"], "expires_at": order["expires_ts"]}

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
                    f"order {txn_ref} is '{order['status']}'; the price lock has lapsed â€” requote",
                    txn_ref=txn_ref, old_amount=order["charge_amount"], new_amount=None)
            db.set_order_status(txn_ref, "paid", body.get("razorpay_order_id"), body.get("payment_ref"))
            chainlog.append(shop_id, "payment_confirmed",
                            f"order {txn_ref} confirmed paid: {order['charge_amount']} paise via "
                            f"Razorpay test order {body.get('razorpay_order_id')} "
                            f"(payment ref {body.get('payment_ref')})",
                            {"txn_ref": txn_ref, "charge_amount": order["charge_amount"],
                             "razorpay_order_id": body.get("razorpay_order_id"),
                             "payment_ref": body.get("payment_ref")})
            return 200, {"txn_ref": txn_ref, "status": "paid",
                         "charge_amount": order["charge_amount"],
                         "razorpay_order_id": body.get("razorpay_order_id"),
                         "payment_ref": body.get("payment_ref")}

        return await idempotent(request, "/confirm-payment", body, handler)

    # -- reservations -------------------------------------------------------

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
        if stock > 0:
            raise errors.NotOutOfStock(
                f"'{item_id}' variant '{variant or '-'}' has {stock} in stock; add it to a cart instead",
                product_id=item_id, variant=variant, in_stock=stock)
        res_id = db.create_reservation(item_id, variant, body["contact_ref"], claims["jti"])
        chainlog.append(shop_id, "reservation_created",
                        f"reserved '{item_id}' variant '{variant or '-'}' for {body['contact_ref']} "
                        f"(restock {restock_date or 'unscheduled'}) under mandate {claims['jti'][:8]}",
                        {"res_id": res_id, "product_id": item_id, "variant": variant,
                         "restock_date": restock_date, "jti": claims["jti"]})
        return {"res_id": res_id, "product_id": item_id, "variant": variant,
                "restock_date": restock_date, "status": "open"}

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
