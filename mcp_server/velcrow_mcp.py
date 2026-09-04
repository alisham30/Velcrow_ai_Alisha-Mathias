"""VelcrowAI as an MCP server - the FOURTH dialect.

The widget speaks to shoppers on the page, WhatsApp in their pocket, ACP to
standards-shaped commerce clients - and this file lets ANY MCP client (Claude
Desktop, or anything else that speaks the Model Context Protocol) shop the
whole VelcrowAI network from its own chat window.

It is deliberately a thin adapter, like the ACP one: every tool here is a
plain HTTP call to the same shop and agent endpoints every other surface
uses. It adds no intelligence and removes no gate:

  - discovery reads the shops' own manifests;
  - carts and quotes go through the same /cart and /order endpoints, under a
    real mandate with hard caps;
  - paying calls the agent's /pay - the cart-bound approval and the wallet's
    five checks run exactly as they do for a widget tap. The MCP client's own
    human-approval UI is the tap: pay_quote() must be called with the EXACT
    amount the quote named, and the wallet refuses anything else.

There is no tool that skips the quote step, no tool that takes a card
number, and no amount this server can charge that the human did not see.

Run (services must be up):
    .venv\\Scripts\\python.exe -m mcp_server.velcrow_mcp

Claude Desktop config (claude_desktop_config.json):
    {"mcpServers": {"velcrow": {
        "command": "C:\\\\...\\\\velcrow-ai\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["-m", "mcp_server.velcrow_mcp"],
        "cwd": "C:\\\\...\\\\velcrow-ai"}}}
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

AGENT = "http://127.0.0.1:8003"
SHOPS: dict[str, str] = {
    "freshkart": "http://127.0.0.1:8001", "loomcraft": "http://127.0.0.1:8002",
    "silkroute": "http://127.0.0.1:8004", "dailymandi": "http://127.0.0.1:8005",
    "urbannest": "http://127.0.0.1:8006", "mitticraft": "http://127.0.0.1:8007",
}
# Hard caps for mandates minted by this surface. An MCP client cannot raise
# them from the outside; a bigger basket needs a smaller basket.
MAX_TOTAL_PAISE = 500_000      # Rs 5,000 per session
MAX_PER_TXN_PAISE = 300_000    # Rs 3,000 per transaction

server = MCPServer(
    "velcrow",
    instructions=(
        "Shop the VelcrowAI merchant network. Browsing is open; BUYING needs "
        "the human logged in - and their number NEVER passes through this "
        "chat: call login_link(), give the human the URL to open in their "
        "browser (they enter phone + WhatsApp code there), then check_login() "
        "once they say they're done. Then: search_products -> create_cart -> "
        "add_to_cart -> get_quote -> show the human the exact total -> "
        "pay_quote with that exact amount. Money only moves through "
        "pay_quote, which runs a five-check wallet; it refuses any amount "
        "that differs from the quote, and refuses everything until login."))

_mandate: dict[str, str] = {}
# Who this session is shopping AS. Verified by a WhatsApp OTP - the same
# login the storefront uses - so every purchase lands in the person's own
# order history and can feed their reminders. Identity gates RECOGNITION
# and, on this surface, the pay tool; spending power still comes only from
# the mandate caps and the human's exact-amount approval.
_identity: dict[str, str] = {}


def _token() -> str:
    if "token" not in _mandate:
        r = httpx.post(f"{AGENT}/mandate",
                       json={"shops": sorted(SHOPS),
                             "max_total_paise": MAX_TOTAL_PAISE,
                             "max_per_txn_paise": MAX_PER_TXN_PAISE},
                       timeout=20).json()
        _mandate["token"] = r["token"]
    return _mandate["token"]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Mandate {_token()}"}


def _shop(shop_id: str) -> str:
    if shop_id not in SHOPS:
        raise ValueError(f"unknown shop '{shop_id}'; use one of {sorted(SHOPS)}")
    return SHOPS[shop_id]


@server.tool()
def login_link() -> dict[str, Any]:
    """Start login WITHOUT the human sharing anything in this chat: give them
    the returned URL to open in their browser. They enter their phone number
    and the WhatsApp code THERE - this conversation never sees either. Then
    call check_login() to see whether they finished."""
    r = httpx.post(f"{AGENT}/auth/link/start", timeout=15)
    body = r.json()
    _identity.clear()
    _identity["link_id"] = body["link_id"]
    return {"open_this_in_your_browser": body["url"],
            "expires_in_seconds": 600,
            "next": "when the human says they logged in, call check_login()"}


@server.tool()
def check_login() -> dict[str, Any]:
    """Whether the human finished the browser login. On success this session
    shops as them - the chat still never sees their number or code."""
    if "link_id" not in _identity:
        return {"logged_in": False, "note": "call login_link() first"}
    st = httpx.get(f"{AGENT}/auth/link/{_identity['link_id']}/status", timeout=15).json()
    if not st.get("done"):
        return {"logged_in": False,
                "note": "not finished yet - ask the human to complete the page"}
    _identity["verified"] = "yes"
    _identity["contact"] = st.get("contact", "")
    _identity["contact_key"] = st.get("contact_key", "")
    return {"logged_in": True,
            "note": "purchases now land in this person's own order history; "
                    "their number stays out of this chat"}


@server.tool()
def whoami() -> dict[str, Any]:
    """Who this session is shopping as - masked: the number itself never
    enters the chat, even after login."""
    if _identity.get("verified") == "yes":
        digits = "".join(ch for ch in _identity.get("contact", "") if ch.isdigit())
        return {"logged_in": True, "as": f"...{digits[-4:]}" if digits else "verified"}
    return {"logged_in": False,
            "note": "browsing is open; buying needs login_link() + check_login()"}


@server.tool()
def list_shops() -> list[dict[str, Any]]:
    """The merchant network: every shop, what it sells, and its capabilities,
    read from each shop's own /.well-known/agent-commerce.json manifest."""
    out = []
    for shop_id, url in SHOPS.items():
        try:
            m = httpx.get(f"{url}/.well-known/agent-commerce.json", timeout=8).json()
            caps = m.get("capabilities", {})
            out.append({"shop_id": shop_id, "merchant": m.get("merchant"),
                        "capabilities": caps if isinstance(caps, dict) else str(caps)})
        except Exception as exc:
            out.append({"shop_id": shop_id, "error": f"unreachable: {exc}"})
    return out


@server.tool()
def search_products(query: str, shop_id: str = "") -> list[dict[str, Any]]:
    """Search one shop, or every shop when shop_id is empty. Results carry
    live prices and stock straight from each shop's catalog."""
    words = [w for w in query.lower().split() if len(w) >= 3]
    words += [w[:-1] for w in list(words) if w.endswith("s") and len(w) > 3]
    targets = {shop_id: _shop(shop_id)} if shop_id else SHOPS
    hits = []
    for sid, url in targets.items():
        try:
            for p in httpx.get(f"{url}/catalog", timeout=8).json():
                hay = f"{p.get('name', '')} {p.get('category', '')} {' '.join(p.get('tags', []))}".lower()
                if not words or any(w in hay for w in words):
                    hits.append({"shop_id": sid, "item_id": p["id"], "name": p["name"],
                                 "price_paise": p["price_paise"],
                                 "in_stock": p.get("stock",
                                                   sum(v.get("stock", 0)
                                                       for v in p.get("variants", []))),
                                 "variants": [v.get("label") for v in p.get("variants", [])]})
        except Exception:
            continue
    return sorted(hits, key=lambda h: h["price_paise"])[:20]


@server.tool()
def create_cart(shop_id: str) -> dict[str, Any]:
    """A fresh cart at one shop. Carts hold goods, never money."""
    r = httpx.post(f"{_shop(shop_id)}/cart", timeout=10).json()
    return {"shop_id": shop_id, "cart_id": r["cart_id"]}


@server.tool()
def add_to_cart(shop_id: str, cart_id: str, item_id: str, qty: int = 1,
                variant: str = "") -> dict[str, Any]:
    """Add an item. If the shelf cannot cover the quantity, the shop adds
    what it has and honestly reports the shortfall."""
    r = httpx.post(f"{_shop(shop_id)}/cart/{cart_id}/fulfil", headers=_auth(),
                   json={"item_id": item_id, "variant": variant, "qty": int(qty),
                         "mode": "add"}, timeout=15)
    body = r.json()
    if r.status_code >= 400:
        return {"error": body.get("why", "refused"), "code": body.get("code")}
    view = httpx.get(f"{_shop(shop_id)}/cart/{cart_id}", timeout=10).json()
    return {"added": body.get("added"), "shortfall": body.get("shortfall", 0),
            "cart_items": [{"item_id": l["item_id"], "qty": l["qty"],
                            "name": l.get("name")} for l in view["items"]],
            "subtotal_paise": view["subtotal_paise"]}


@server.tool()
def get_quote(shop_id: str, cart_id: str) -> dict[str, Any]:
    """Price the cart into a firm quote: coupons applied by the shop's own
    engine, stock held under a 5-minute price lock. SHOW THE HUMAN the
    charge_amount_paise - pay_quote will demand it verbatim."""
    if _identity.get("verified") != "yes":
        return {"error": "login required before quoting: ask the human for their phone "
                         "number, call login(phone_number), then verify_login(code)"}
    r = httpx.post(f"{_shop(shop_id)}/order",
                   headers={**_auth(), "Idempotency-Key": f"mcp-{uuid.uuid4().hex[:10]}"},
                   json={"cart_id": cart_id, "assisted": True, "source": "mcp",
                         "contact": _identity.get("contact", "")},
                   timeout=20)
    body = r.json()
    if r.status_code >= 400:
        return {"error": body.get("why", "refused"), "code": body.get("code")}
    return {"txn_ref": body["txn_ref"], "charge_amount_paise": body["charge_amount"],
            "coupon": body["coupon"]["codes"], "line_items": body["line_items"],
            "price_lock_seconds": 300,
            "note": "pay_quote(shop_id, txn_ref, exact_amount_paise) completes it; "
                    "any other amount is refused by the wallet"}


@server.tool()
def pay_quote(shop_id: str, txn_ref: str, exact_amount_paise: int) -> dict[str, Any]:
    """Pay a quote - the ONLY tool that moves money, and it moves it through
    the same wallet as every other surface: mandate verified, caps enforced,
    cart-bound approval signed over exactly this amount, five ordered checks,
    Razorpay test order. An amount that differs from the quote is refused."""
    if _identity.get("verified") != "yes":
        return {"error": "login required: nothing is paid on this surface without a "
                         "verified person behind it - login(phone_number) + verify_login(code)"}
    order = httpx.get(f"{_shop(shop_id)}/order/{txn_ref}", timeout=10).json()
    if order.get("status") != "pending":
        return {"error": f"quote is '{order.get('status')}', not pending - get a fresh quote"}
    r = httpx.post(f"{AGENT}/pay",
                   json={"shop_id": shop_id, "shop_url": _shop(shop_id),
                         "txn_ref": txn_ref, "mandate_token": _token(),
                         "approved_amount_paise": int(exact_amount_paise),
                         "approved_items": order["line_items"]}, timeout=60)
    body = r.json()
    if r.status_code >= 400:
        return {"refused": body.get("why", "the wallet said no"), "code": body.get("code"),
                "charged": 0}
    return {"paid": True, "txn_ref": txn_ref, "amount_paise": int(exact_amount_paise),
            "razorpay_order_id": body.get("razorpay_order_id"),
            "confirmed": body.get("confirmed")}


@server.tool()
def order_status(shop_id: str, txn_ref: str) -> dict[str, Any]:
    """The shop's own record of an order."""
    o = httpx.get(f"{_shop(shop_id)}/order/{txn_ref}", timeout=10).json()
    return {"status": o.get("status"), "charge_amount_paise": o.get("charge_amount"),
            "line_items": o.get("line_items")}


if __name__ == "__main__":
    server.run()
