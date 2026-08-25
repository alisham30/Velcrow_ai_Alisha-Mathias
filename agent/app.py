r"""VelcrowAI service :8003 (spec 2, 6.3, 12).

Phase 2 brought this up in trust-core-only form: mandate issue, cart-bound
approval signed at the moment of the human's tap, wallet.pay, confirm with
the shop. Phase 4 grows it into the agent service proper — the tool-calling
loop, its SSE trace, and the widget bundle both storefronts embed.

Buyer/merchant separation is absolute: this service holds the buyer-side
keys; the shop never signs anything on the buyer's behalf. Money still moves
only through common/wallet.py, and the agent has no payment tool until
Phase 5.

Run: python -m uvicorn agent.app:create_app --factory --port 8003
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from agent import llm, runtime
from common import approval, chainlog, errors, mandate, wallet

# The merchants that have installed the widget. data-shop on the script tag
# selects one; the widget learns everything else from /agent/config.
INSTALLED_SHOPS: dict[str, dict[str, Any]] = {
    "grocery": {"shop_id": "freshkart", "name": "FreshKart", "category": "grocery",
                "url": "http://127.0.0.1:8001"},
    "apparel": {"shop_id": "loomcraft", "name": "Loomcraft", "category": "apparel",
                "url": "http://127.0.0.1:8002"},
}
WIDGET_JS = Path(__file__).parent / "static" / "velcrow.js"

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
        return {"service": "velcrow-agent", "form": "trust-core + tool-calling agent",
                "phase": 4, "model": llm.MODEL,
                "tools": [t["function"]["name"] for t in llm.TOOLS],
                "shops": sorted(INSTALLED_SHOPS)}

    # -- widget (spec 6.3: one script tag, served by :8003) ------------------

    @app.get("/velcrow.js")
    def widget_bundle() -> FileResponse:
        return FileResponse(WIDGET_JS, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/agent/config")
    def widget_config(shop: str = "grocery") -> dict[str, Any]:
        """What the widget needs to attach itself to the host page."""
        installed = INSTALLED_SHOPS.get(shop)
        if installed is None:
            raise errors.BadRequest(
                f"no shop '{shop}' has installed this widget; "
                f"expected one of {sorted(INSTALLED_SHOPS)}")
        return {"shop": shop, "shop_id": installed["shop_id"], "shop_name": installed["name"],
                "api_base": installed["url"], "cart_storage_key": f"velcrow-cart-{installed['shop_id']}"}

    # -- agent loop (spec 6.3) ----------------------------------------------

    @app.post("/agent/chat", status_code=202)
    async def agent_chat(request: Request) -> dict[str, Any]:
        """Start one shopper turn. Returns a run id; the trace streams on
        /agent/run/{id}/events."""
        body = await json_body(request)
        shop_key = body.get("shop", "grocery")
        installed = INSTALLED_SHOPS.get(shop_key)
        if installed is None:
            raise errors.BadRequest(f"unknown shop '{shop_key}'")
        message = str(body.get("message", "")).strip()
        if not message:
            raise errors.BadRequest("message is required")
        cart_id = body.get("cart_id")
        if not cart_id:
            raise errors.BadRequest("cart_id is required — the agent works the page's own cart")

        # The agent operates under a session mandate like any other buyer, so
        # its limits are the same ones the wallet will enforce at checkout.
        token = body.get("mandate_token")
        if token:
            claims = mandate.verify(token)
        else:
            token = mandate.issue(DEFAULT_MAX_TOTAL_PAISE, DEFAULT_MAX_PER_TXN_PAISE,
                                  [installed["shop_id"]])
            claims = mandate.verify(token)

        run = runtime.new_run(installed["shop_id"])
        chainlog.append("buyer", "agent_turn_started",
                        f"shopper asked the agent at {installed['name']}: {message!r}",
                        {"run_id": run.run_id, "shop_id": installed["shop_id"], "cart_id": cart_id})
        asyncio.create_task(runtime.run_turn(
            run, installed, cart_id, message, body.get("history") or [], claims))
        return {"run_id": run.run_id, "shop_id": installed["shop_id"], "mandate_token": token}

    @app.get("/agent/run/{run_id}/events")
    async def agent_events(run_id: str) -> StreamingResponse:
        """SSE trace of the turn: every tool call, its args, the model's
        reason, a result summary and its latency (spec 6.5)."""
        run = runtime.RUNS.get(run_id)
        if run is None:
            raise errors.NotFound(f"no such run '{run_id}'", run_id=run_id)

        async def stream() -> Any:
            queue = run.subscribe()
            while True:
                event = await queue.get()
                if event is None:
                    yield "event: done\ndata: {}\n\n"
                    return
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/agent/run/{run_id}")
    def agent_run(run_id: str) -> dict[str, Any]:
        """The finished trace, for inspection from a terminal."""
        run = runtime.RUNS.get(run_id)
        if run is None:
            raise errors.NotFound(f"no such run '{run_id}'", run_id=run_id)
        return {"run_id": run_id, "shop_id": run.shop_id, "done": run.done, "events": run.events}

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
