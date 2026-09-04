r"""ACP checkout adapter (spec 6.7): a standards-shaped surface over the cart.

Implements the Agentic Commerce Protocol checkout-session API, version
2026-04-17, read from the live published spec (github.com/agentic-commerce-
protocol) on 2026-08-27 - endpoint paths, request shapes, status values and
error codes are taken from that document, not from this repository's spec or
from memory, per VELCROW_ROUND2_1.md 6.7's own rule.

What the live spec says, and what this file therefore does:

  POST /checkout_sessions                    create - a priced quote, no holds
  GET  /checkout_sessions/{id}               retrieve
  POST /checkout_sessions/{id}               update - "Replace all line items"
  POST /checkout_sessions/{id}/complete      pay - order comes into being HERE
  POST /checkout_sessions/{id}/cancel        cancel

  - Request line items are Item objects: {id} only. There is NO quantity
    field on the request side in 2026-04-17; quantity is expressed by
    repeating the id, and the merchant consolidates into response LineItems
    that DO carry quantity. (Verified against the JSON Schema and the
    multi-item example, because it is surprising.)
  - Statuses used here: not_ready_for_payment, ready_for_payment, completed,
    canceled - the subset of the spec's enum this merchant can honestly emit.
  - Money is expressed in minor units (paise), matching the spec's
    unit_amount definition and this project's only money representation.
  - Errors inside a session are Message objects with the spec's code enum
    (out_of_stock, invalid, ...); transport-level failures are the spec's
    {type, code, message} Error shape.

What this file deliberately does NOT do:

  - Move money on its own. complete() calls the SAME place_order and
    settle_payment the native surface uses, behind the SAME mandate check.
    The adapter is a dialect, not a second door (spec 5, 7.9).
  - Hold stock at session create. A session is a quote; stock moves when the
    order is placed, under the native price lock.
  - Claim more of ACP than is real: payment is a test-mode simulation that
    accepts a delegated 'spt' credential shape. The manifest says exactly
    this. We implement the checkout surface; claims of full ACP compliance
    (delegate-payment vaulting, webhooks, signatures) would be false and are
    not made.

Variants: ACP Items carry no variant field (additionalProperties: false), so
this merchant defines its item ids for the ACP surface as
"<product_id>" or "<product_id>::<variant label>" - id formats are the
merchant's to define, and the manifest documents it.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import chainlog, errors, mandate

ACP_VERSION = "2026-04-17"

# The spec's session statuses this merchant can honestly emit.
NOT_READY = "not_ready_for_payment"
READY = "ready_for_payment"
COMPLETED = "completed"
CANCELED = "canceled"


def _err(status: int, code: str, message: str) -> JSONResponse:
    """Transport-level Error, in the spec's {type, code, message} shape."""
    return JSONResponse(status_code=status,
                        content={"type": "invalid_request", "code": code, "message": message})


def _split_item_id(raw: str) -> tuple[str, str]:
    """'<product_id>' or '<product_id>::<variant>' -> (product_id, variant)."""
    if "::" in raw:
        pid, _, variant = raw.partition("::")
        return pid, variant
    return raw, ""


def mount(app: FastAPI, ctx: dict[str, Any]) -> None:
    """Attach the ACP routes. `ctx` carries the shop's own primitives so the
    adapter reuses them instead of growing rivals:

        db, shop_id, caps, all_coupons, coupon_engine,
        json_body, idempotent, place_order, settle_payment
    """
    db = ctx["db"]
    shop_id: str = ctx["shop_id"]
    caps: dict[str, Any] = ctx["caps"]
    all_coupons: Callable[[], list[dict[str, Any]]] = ctx["all_coupons"]
    coupon_engine = ctx["coupon_engine"]
    json_body = ctx["json_body"]
    idempotent = ctx["idempotent"]
    place_order = ctx["place_order"]
    settle_payment = ctx["settle_payment"]

    def bearer_mandate(request: Request) -> dict[str, Any]:
        """ACP speaks 'Authorization: Bearer <token>'; the token a buyer
        presents here is a VelcrowAI mandate, verified exactly as the native
        surface verifies it. Same signature, same caps, same shops claim."""
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise errors.MandateInvalid(
                "missing 'Authorization: Bearer <mandate jwt>' header on the ACP surface")
        claims = mandate.verify(auth[len("Bearer "):])
        if shop_id not in claims.get("shops", []):
            raise errors.ShopNotPermitted(
                f"mandate does not permit shop '{shop_id}'", shop_id=shop_id)
        return claims

    def consolidate(raw_items: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
        """Request Items carry no quantity (live spec, 2026-04-17): repeating
        an id is the quantity. Order of first appearance is kept."""
        counts: dict[tuple[str, str], int] = {}
        for it in raw_items:
            pid, variant = _split_item_id(str(it.get("id") or ""))
            key = (pid, variant)
            counts[key] = counts.get(key, 0) + 1
        return [(pid, variant, qty) for (pid, variant), qty in counts.items()]

    def set_lines(cart_id: str, wanted: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
        """Replace the cart's lines ('Replace all line items' - the spec's own
        wording for update). Returns per-line problems as spec Messages."""
        db.clear_cart(cart_id)
        messages: list[dict[str, Any]] = []
        for i, (pid, variant, qty) in enumerate(wanted):
            p = db.product(pid)
            if p is None:
                messages.append(_message_error("invalid", f"$.line_items[{i}]",
                                               f"no such item '{pid}'"))
                continue
            row = db.stock_row(pid, variant)
            if row is None:
                messages.append(_message_error("invalid", f"$.line_items[{i}]",
                                               f"'{pid}' has no variant '{variant or '-'}'"))
                continue
            stock, restock_date = row
            if qty > stock:
                back = f"; restock expected {restock_date}" if restock_date else ""
                messages.append(_message_error(
                    "out_of_stock", f"$.line_items[{i}]",
                    f"'{p['name']}'{' [' + variant + ']' if variant else ''} has {stock} in "
                    f"stock, {qty} requested{back}"))
                if stock <= 0:
                    continue        # nothing to put in the cart at all
                qty = stock         # the shelf's worth goes in; the message stands
            db.upsert_line(cart_id, pid, variant, qty)
        return messages

    def _message_error(code: str, param: str, content: str) -> dict[str, Any]:
        return {"type": "error", "code": code, "param": param,
                "content_type": "plain", "content": content}

    def session_view(sess: dict[str, Any],
                     messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The CheckoutSession object, priced fresh from the cart every time.
        Coupon maths comes from the same engine as the native surface, so the
        two protocols can never quote different totals for one basket."""
        if sess["status"] == COMPLETED and sess["order_txn"]:
            return _completed_view(sess)
        lines = []
        for it in db.cart_items(sess["cart_id"]):
            p = db.product(it["item_id"])
            if p is None:
                continue
            unit = p["price_paise"]
            acp_id = it["item_id"] + (f"::{it['variant']}" if it["variant"] else "")
            lines.append({
                "id": f"line_{it['item_id']}_{it['variant'] or 'flat'}",
                "item": {"id": acp_id, "name": p["name"], "unit_amount": unit},
                "quantity": int(it["qty"]),
                "name": p["name"],
                "unit_amount": unit,
                "totals": [{"type": "items_base_amount", "display_text": "Item(s) total",
                            "amount": unit * int(it["qty"])}],
            })

        priced = [{"item_id": l["item"]["id"].split("::")[0],
                   "variant": (l["item"]["id"].split("::") + [""])[1],
                   "qty": l["quantity"], "unit_price_paise": l["unit_amount"],
                   "category": (db.product(l["item"]["id"].split("::")[0]) or {}).get("category", "")}
                  for l in lines]
        best = coupon_engine.evaluate(priced, all_coupons())["best"] if priced else \
            {"codes": [], "discount_paise": 0, "net_total_paise": 0}
        subtotal = sum(l["quantity"] * l["unit_amount"] for l in lines)

        totals = [{"type": "items_base_amount", "display_text": "Item(s) total",
                   "amount": subtotal}]
        if best["discount_paise"]:
            totals.append({"type": "discount",
                           "display_text": " + ".join(best["codes"]) or "Discount",
                           "amount": -best["discount_paise"]})
        totals.append({"type": "total", "display_text": "Total",
                       "amount": subtotal - best["discount_paise"]})

        messages = messages or []
        status = sess["status"]
        if status not in (COMPLETED, CANCELED):
            blocking = any(m["type"] == "error" for m in messages)
            status = NOT_READY if (blocking or not lines) else READY
            if status != sess["status"]:
                db.acp_update_session(sess["session_id"], status=status)

        return {
            "id": sess["session_id"],
            "status": status,
            "currency": "inr",
            "line_items": lines,
            "totals": totals,
            "messages": messages,
            "links": [{"type": "terms_of_use", "title": "Demo shop - test mode only",
                       "url": "https://razorpay.com/docs/payments/test-mode/"}],
        }

    def _completed_view(sess: dict[str, Any]) -> dict[str, Any]:
        """A completed session reports the ORDER it became. Its cart was
        emptied at settlement (a bought basket stops being a basket), so
        pricing from the cart here reported an honest sale as zero."""
        order = db.get_order(sess["order_txn"])
        assert order is not None
        lines = []
        for li in order["line_items"]:
            p = db.product(li["item_id"])
            acp_id = li["item_id"] + (f"::{li['variant']}" if li.get("variant") else "")
            lines.append({
                "id": f"line_{li['item_id']}_{li.get('variant') or 'flat'}",
                "item": {"id": acp_id, "name": (p or {}).get("name", li["item_id"]),
                         "unit_amount": li["unit_price_paise"]},
                "quantity": int(li["qty"]),
                "name": (p or {}).get("name", li["item_id"]),
                "unit_amount": li["unit_price_paise"],
                "totals": [{"type": "items_base_amount", "display_text": "Item(s) total",
                            "amount": li["unit_price_paise"] * int(li["qty"])}],
            })
        subtotal = sum(l["quantity"] * l["unit_amount"] for l in lines)
        coupon = order.get("coupon") or {}
        totals = [{"type": "items_base_amount", "display_text": "Item(s) total",
                   "amount": subtotal}]
        if coupon.get("discount_paise"):
            totals.append({"type": "discount",
                           "display_text": " + ".join(coupon.get("codes", [])) or "Discount",
                           "amount": -coupon["discount_paise"]})
        totals.append({"type": "total", "display_text": "Total",
                       "amount": order["charge_amount"]})
        return {"id": sess["session_id"], "status": COMPLETED, "currency": "inr",
                "line_items": lines, "totals": totals, "messages": [],
                "links": [{"type": "terms_of_use", "title": "Demo shop - test mode only",
                           "url": "https://razorpay.com/docs/payments/test-mode/"}]}

    # -- routes, at the spec's own paths ------------------------------------

    @app.post("/checkout_sessions", status_code=201)
    async def acp_create(request: Request) -> JSONResponse:
        body = await json_body(request)
        raw = body.get("line_items")
        if not isinstance(raw, list) or not raw:
            return _err(400, "invalid", "line_items is required and must have at least 1 item")

        def handler() -> tuple[int, dict[str, Any]]:
            cart_id = db.create_cart()
            session_id = "acp_cs_" + uuid.uuid4().hex[:12]
            db.acp_create_session(session_id, cart_id)
            if body.get("buyer"):
                db.acp_update_session(session_id, buyer=json.dumps(body["buyer"]))
            messages = set_lines(cart_id, consolidate(raw))
            sess = db.acp_session(session_id)
            view = session_view(sess, messages)
            chainlog.append(shop_id, "acp_session_created",
                            f"ACP checkout session {session_id} created with "
                            f"{len(view['line_items'])} line(s), status {view['status']}"
                            + (f", {len(messages)} problem(s) reported" if messages else ""),
                            {"session_id": session_id, "cart_id": cart_id,
                             "status": view["status"], "acp_version": ACP_VERSION})
            return 201, view

        return await idempotent(request, "/checkout_sessions", body, handler)

    @app.get("/checkout_sessions/{session_id}")
    def acp_get(session_id: str) -> JSONResponse:
        sess = db.acp_session(session_id)
        if sess is None:
            return _err(404, "not_found", f"no such checkout session '{session_id}'")
        return JSONResponse(session_view(sess))

    @app.post("/checkout_sessions/{session_id}")
    async def acp_update(session_id: str, request: Request) -> JSONResponse:
        body = await json_body(request)
        sess = db.acp_session(session_id)
        if sess is None:
            return _err(404, "not_found", f"no such checkout session '{session_id}'")
        if sess["status"] in (COMPLETED, CANCELED):
            return _err(409, "conflict", f"session is {sess['status']} and cannot change")
        messages: list[dict[str, Any]] = []
        if isinstance(body.get("line_items"), list):
            messages = set_lines(sess["cart_id"], consolidate(body["line_items"]))
        if body.get("buyer"):
            db.acp_update_session(session_id, buyer=json.dumps(body["buyer"]))
        return JSONResponse(session_view(db.acp_session(session_id), messages))

    @app.post("/checkout_sessions/{session_id}/complete")
    async def acp_complete(session_id: str, request: Request) -> JSONResponse:
        body = await json_body(request)
        sess = db.acp_session(session_id)
        if sess is None:
            return _err(404, "not_found", f"no such checkout session '{session_id}'")

        # The spec requires buyer and payment_data; this merchant additionally
        # requires the bearer mandate, because nothing orders here without one.
        buyer = body.get("buyer") or {}
        pay = body.get("payment_data") or {}
        cred = ((pay.get("instrument") or {}).get("credential") or {})
        if not buyer.get("email"):
            return _err(400, "missing", "buyer.email is required")
        if not cred.get("token"):
            return _err(400, "missing",
                        "payment_data.instrument.credential.token is required")
        try:
            claims = bearer_mandate(request)
        except errors.VelcrowError as exc:
            chainlog.append(shop_id, "acp_complete_refused", exc.why,
                            {"session_id": session_id, "code": exc.code})
            return _err(401, exc.code.lower(), exc.why)

        def handler() -> tuple[int, dict[str, Any]]:
            # Status checks live INSIDE the idempotent handler, so a replayed
            # Idempotency-Key returns the stored completion instead of a 409
            # for a session its own first call completed.
            live = db.acp_session(session_id)
            if live["status"] == COMPLETED:
                return 409, {"type": "invalid_request", "code": "conflict",
                             "message": "session is already completed"}
            if live["status"] == CANCELED:
                return 409, {"type": "invalid_request", "code": "conflict",
                             "message": "session was canceled"}
            view = session_view(live)
            if view["status"] != READY:
                code = ("out_of_stock" if any(m["code"] == "out_of_stock"
                                              for m in view["messages"]) else "invalid")
                return 409, {"type": "invalid_request", "code": code,
                             "message": "session is not ready for payment; resolve its "
                                        "messages first"}
            # The same door as every other order. place_order re-checks stock,
            # prices with the same coupon engine, enforces the mandate caps in
            # code and holds stock under the native price lock; settle_payment
            # settles rescued reservations and recovered demand identically.
            try:
                _, placed = place_order(
                    {"cart_id": sess["cart_id"], "assisted": True, "source": "acp",
                     "shopper_ref": f"acp_{session_id}",
                     "contact": buyer.get("email", "")}, claims)
                _, paid = settle_payment(
                    {"txn_ref": placed["txn_ref"],
                     "razorpay_order_id": f"order_acp_{uuid.uuid4().hex[:10]}",
                     "payment_ref": str(cred["token"])[:64]})
            except errors.VelcrowError as exc:
                chainlog.append(shop_id, "acp_complete_refused", exc.why,
                                {"session_id": session_id, "code": exc.code})
                return 402 if exc.code == "OVER_CAP" else 400, {
                    "type": "invalid_request", "code": exc.code.lower(), "message": exc.why}

            db.acp_update_session(session_id, status=COMPLETED,
                                  order_txn=placed["txn_ref"],
                                  buyer=json.dumps(buyer))
            chainlog.append(shop_id, "acp_session_completed",
                            f"ACP session {session_id} completed: order {placed['txn_ref']} "
                            f"for {placed['charge_amount']} paise, settled through the same "
                            "place_order/settle_payment path as every native sale",
                            {"session_id": session_id, "txn_ref": placed["txn_ref"],
                             "charge_amount": placed["charge_amount"]})
            done = session_view(db.acp_session(session_id))
            done["order"] = {
                "type": "order",
                "id": placed["txn_ref"],
                "checkout_session_id": session_id,
                "order_number": placed["txn_ref"],
                "permalink_url": f"{str(request.base_url).rstrip('/')}/order/{placed['txn_ref']}",
                "status": "confirmed",
                "confirmation": {"confirmation_number": placed["txn_ref"],
                                 "confirmation_email_sent": False},
                "totals": done["totals"],
            }
            return 200, done

        return await idempotent(request, f"/checkout_sessions/{session_id}/complete",
                                body, handler)

    @app.post("/checkout_sessions/{session_id}/cancel")
    def acp_cancel(session_id: str) -> JSONResponse:
        sess = db.acp_session(session_id)
        if sess is None:
            return _err(404, "not_found", f"no such checkout session '{session_id}'")
        if sess["status"] == COMPLETED:
            return _err(409, "conflict", "a completed session cannot be canceled")
        db.acp_update_session(session_id, status=CANCELED)
        db.clear_cart(sess["cart_id"])
        chainlog.append(shop_id, "acp_session_canceled",
                        f"ACP checkout session {session_id} canceled; its cart was emptied "
                        "and nothing was charged", {"session_id": session_id})
        sess = db.acp_session(session_id)
        return JSONResponse(session_view(sess))


def manifest_block(caps: dict[str, Any]) -> dict[str, Any]:
    """The `checkout` block the shop's manifest declares alongside the native
    surface. Says exactly what is implemented and no more (spec 13: claim
    influence and interoperability, never equivalence)."""
    return {
        "protocol": "acp",
        "version": ACP_VERSION,
        "spec": "https://github.com/agentic-commerce-protocol/agentic-commerce-protocol",
        "base_url": "/",
        "endpoints": {
            "create": "POST /checkout_sessions",
            "get": "GET /checkout_sessions/{checkout_session_id}",
            "update": "POST /checkout_sessions/{checkout_session_id}",
            "complete": "POST /checkout_sessions/{checkout_session_id}/complete",
            "cancel": "POST /checkout_sessions/{checkout_session_id}/cancel",
        },
        "item_id_format": "<product_id> or <product_id>::<variant label>",
        "quantity": "repeat the item id; request Items carry no quantity field in "
                    f"{ACP_VERSION} and the merchant consolidates",
        "auth": "Authorization: Bearer <VelcrowAI mandate JWT> - required on complete",
        "payment": {"mode": "razorpay_test_simulation",
                    "accepts": {"instrument.credential.type": "spt"},
                    "note": "checkout surface only; delegate-payment vaulting, webhooks "
                            "and signatures are not implemented"},
        "amounts": "integer minor units (paise)",
    }
