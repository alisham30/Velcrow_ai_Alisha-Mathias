r"""VelcrowAI service :8003 — Phase 2 brings it up in TRUST-CORE-ONLY form
(spec 2, 12): mandate issue, cart-bound approval signed at the moment of the
human's tap, wallet.pay, confirm with the shop. No widget, no LLM, no
scheduler — Phase 4 extends this same service.

Buyer/merchant separation is absolute: this service holds the buyer-side
keys; the shop never signs anything on the buyer's behalf.

Run: python -m uvicorn agent.app:create_app --factory --port 8003
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from common import approval, chainlog, errors, mandate, wallet

FRONTEND_ORIGINS = ["http://localhost:5173", "http://localhost:5174", "http://localhost:5175",
                    "http://127.0.0.1:5173", "http://127.0.0.1:5174", "http://127.0.0.1:5175"]
DEFAULT_MAX_TOTAL_PAISE = 500000   # Rs 5,000 session budget
DEFAULT_MAX_PER_TXN_PAISE = 300000  # Rs 3,000 per transaction
KNOWN_SHOPS = {"freshkart", "loomcraft"}


def _post_confirm(shop_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{shop_url}/confirm-payment", json=payload,
                      headers={"Idempotency-Key": f"confirm-{payload['txn_ref']}"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def create_app() -> FastAPI:
    load_dotenv()
    app = FastAPI(title="VelcrowAI trust service", version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(errors.VelcrowError)
    async def _velcrow_error(_req: Request, exc: errors.VelcrowError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.payload())

    async def json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            raise errors.BadRequest("request body is not valid JSON")
        if not isinstance(body, dict):
            raise errors.BadRequest("request body must be a JSON object")
        return body

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"service": "velcrow-trust", "form": "trust-core-only", "phase": 2}

    @app.post("/mandate", status_code=201)
    async def issue_mandate(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        shops = body.get("shops") or []
        if not shops or not all(s in KNOWN_SHOPS for s in shops):
            raise errors.BadRequest(f"shops must be a non-empty subset of {sorted(KNOWN_SHOPS)}")
        max_total = int(body.get("max_total_paise", DEFAULT_MAX_TOTAL_PAISE))
        max_per_txn = int(body.get("max_per_txn_paise", DEFAULT_MAX_PER_TXN_PAISE))
        ttl = int(body.get("ttl_seconds", 3600))
        token = mandate.issue(max_total, max_per_txn, shops, ttl_seconds=ttl)
        claims = mandate.verify(token)
        chainlog.append("buyer", "mandate_issued",
                        f"session mandate issued for {shops}: max_total {max_total} paise, "
                        f"max_per_txn {max_per_txn} paise, valid {ttl}s",
                        {"jti": claims["jti"], "shops": shops,
                         "max_total": max_total, "max_per_txn": max_per_txn})
        return {"token": token, "jti": claims["jti"], "max_total_paise": max_total,
                "max_per_txn_paise": max_per_txn, "shops": shops, "expires_at": claims["exp"]}

    @app.post("/pay")
    async def pay(request: Request) -> dict[str, Any]:
        """Called at the moment the human taps Approve. Signs the cart-bound
        approval over exactly what the human saw, then runs the wallet."""
        body = await json_body(request)
        for field in ("shop_id", "shop_url", "txn_ref", "mandate_token",
                      "approved_amount_paise", "approved_items"):
            if field not in body:
                raise errors.BadRequest(f"missing field '{field}'")
        shop_id: str = body["shop_id"]
        amount = body["approved_amount_paise"]
        if not isinstance(amount, int) or amount <= 0:
            raise errors.BadRequest("approved_amount_paise must be positive integer paise")
        claims = mandate.verify(body["mandate_token"])
        appr = approval.issue(shop_id, body["txn_ref"], body["approved_items"], amount,
                              claims["jti"])
        chainlog.append("buyer", "approval_signed",
                        f"human approved basket {body['txn_ref']} from {shop_id} at {amount} paise; "
                        f"cart-bound approval signed (5-minute window, single-use nonce)",
                        {"txn_ref": body["txn_ref"], "shop_id": shop_id, "amount_paise": amount,
                         "jti": claims["jti"]})
        result = wallet.pay(body["mandate_token"], appr, shop_id, amount, body["txn_ref"],
                            shop_url=body["shop_url"])
        try:
            confirm = _post_confirm(body["shop_url"], {
                "txn_ref": body["txn_ref"],
                "razorpay_order_id": result["razorpay_order_id"],
                "payment_ref": result["payment_ref"]})
            confirmed = confirm.get("status") == "paid"
        except Exception as exc:
            confirmed = False
            chainlog.append("buyer", "confirm_failed",
                            f"payment {result['payment_ref']} succeeded but the shop's "
                            f"/confirm-payment failed: {exc}; shop chain still holds the wallet entry",
                            {"txn_ref": body["txn_ref"], "shop_id": shop_id})
        return {"txn_ref": body["txn_ref"], "shop_id": shop_id, "amount_paise": amount,
                "razorpay_order_id": result["razorpay_order_id"],
                "payment_ref": result["payment_ref"], "confirmed": confirmed}

    return app
