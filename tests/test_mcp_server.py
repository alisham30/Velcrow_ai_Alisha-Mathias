"""The MCP dialect: any MCP client can shop the network, money unchanged.

What these tests pin: the tools are thin HTTP over the same endpoints as
every other surface; the quote -> exact-amount flow is enforced by the
wallet (a wrong amount is refused with PRICE_CHANGED); and pay_quote is the
only tool that can reach /pay at all.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from common import wallet

REPO = Path(__file__).resolve().parent.parent


class FakeRazorpayClient:
    created: list[dict[str, Any]] = []

    def __init__(self, auth: tuple[str, str]) -> None:
        self.order = self

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        FakeRazorpayClient.created.append(payload)
        return {"id": f"order_FAKE{len(FakeRazorpayClient.created):03d}", **payload}


class Router:
    """Dispatch the adapter's absolute-URL calls onto in-process TestClients."""

    def __init__(self, agent_client, shop_client) -> None:
        self.agent, self.shop = agent_client, shop_client

    def _split(self, url: str):
        client = self.agent if ":8003" in url else self.shop
        return client, "/" + url.split("/", 3)[3]

    def get(self, url, **kw):
        kw.pop("timeout", None)
        c, path = self._split(url)
        return c.get(path, **kw)

    def post(self, url, **kw):
        kw.pop("timeout", None)
        c, path = self._split(url)
        return c.post(path, **kw)


@pytest.fixture
def mcp(env, freshkart, monkeypatch):
    from fastapi.testclient import TestClient

    from agent.app import create_app as agent_app
    from mcp_server import velcrow_mcp as v

    agent = TestClient(agent_app(), raise_server_exceptions=True)
    router = Router(agent, freshkart)
    monkeypatch.setattr(v, "httpx", router)
    monkeypatch.setattr(v, "SHOPS", {"freshkart": "http://127.0.0.1:8001"})
    v._mandate.clear()
    v._identity.clear()
    FakeRazorpayClient.created = []
    monkeypatch.setattr(wallet, "razorpay", SimpleNamespace(Client=FakeRazorpayClient))
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: freshkart.get(f"/order/{txn}").json())
    return v


def _login(v):
    """The REAL out-of-band flow: the assistant gets a link; 'the browser'
    (simulated by direct endpoint calls) enters the number and the OTP; the
    chat side only ever polls check_login. Code read from the honest outbox
    (transport unconfigured in tests)."""
    from agent import outreach

    link = v.login_link()
    assert "127.0.0.1:8003/auth/link/" in link["open_this_in_your_browser"]
    assert v.check_login()["logged_in"] is False       # not finished yet
    # the person, on their own page:
    link_id = link["open_this_in_your_browser"].rsplit("/", 1)[1]
    outreach.start_login("9821158848")
    code = [m for m in outreach.outbox(5) if m["kind"] == "login_code"][0]["body"].split(" ", 1)[0]
    outreach.verify_login("9821158848", code, link_id=link_id)
    got = v.check_login()
    assert got["logged_in"] is True
    return got


def test_nothing_is_paid_without_a_login(mcp):
    """Browsing is open; buying is not. Both the quote and the pay tool
    refuse until a WhatsApp-OTP login has named whose purchase this is."""
    cart = mcp.create_cart("freshkart")
    mcp.add_to_cart("freshkart", cart["cart_id"], "lemons-1kg", 1)
    q = mcp.get_quote("freshkart", cart["cart_id"])
    assert "login required" in q["error"]
    out = mcp.pay_quote("freshkart", "txn_whatever", 100)
    assert "login required" in out["error"]
    assert FakeRazorpayClient.created == []


def test_the_login_flow_attaches_purchases_to_the_person(mcp, freshkart):
    _login(mcp)
    cart = mcp.create_cart("freshkart")
    mcp.add_to_cart("freshkart", cart["cart_id"], "lemons-1kg", 2)
    quote = mcp.get_quote("freshkart", cart["cart_id"])
    paid = mcp.pay_quote("freshkart", quote["txn_ref"], quote["charge_amount_paise"])
    assert paid["paid"] is True
    # settle explicitly (under pytest /pay's confirm can't dial loopback)
    freshkart.post("/confirm-payment",
                   json={"txn_ref": quote["txn_ref"],
                         "razorpay_order_id": paid["razorpay_order_id"],
                         "payment_ref": "pay_mcp_test"})
    hist = freshkart.get("/orders/history?contact_key=phone:9821158848").json()
    mine = next(o for o in hist["orders"] if o["txn_ref"] == quote["txn_ref"])
    assert mine["source"] == "mcp"          # on THEIR orders page, named door


def test_a_wrong_code_cannot_complete_a_link_session(mcp):
    """The ONLY thing that can mark a link login done is a successful OTP
    verification - there is no completion call to forge."""
    from agent import outreach

    link_id = mcp.login_link()["open_this_in_your_browser"].rsplit("/", 1)[1]
    outreach.start_login("9821158848")
    import pytest as _pytest

    from common import errors
    with _pytest.raises(errors.VelcrowError):
        outreach.verify_login("9821158848", "000000", link_id=link_id)
    assert mcp.check_login()["logged_in"] is False


def test_search_cart_quote_and_exact_amount_pays(mcp):
    _login(mcp)
    hits = mcp.search_products("lemons")
    assert hits and hits[0]["item_id"] == "lemons-1kg"
    cart = mcp.create_cart("freshkart")
    added = mcp.add_to_cart("freshkart", cart["cart_id"], "lemons-1kg", 2)
    assert added["added"] == 2 and added["subtotal_paise"] > 0
    quote = mcp.get_quote("freshkart", cart["cart_id"])
    assert quote["txn_ref"] and quote["charge_amount_paise"] > 0

    paid = mcp.pay_quote("freshkart", quote["txn_ref"], quote["charge_amount_paise"])
    assert paid["paid"] is True
    assert len(FakeRazorpayClient.created) == 1
    # confirmed=False here only because /pay's confirm step dials the shop over
    # real loopback HTTP, which does not exist under pytest; the live services
    # confirm (verified in the dry run), and settlement is tested elsewhere.
    assert paid["razorpay_order_id"].startswith("order_FAKE")


def test_a_wrong_amount_is_refused_by_the_wallet(mcp):
    _login(mcp)
    cart = mcp.create_cart("freshkart")
    mcp.add_to_cart("freshkart", cart["cart_id"], "honey-500g", 1)
    quote = mcp.get_quote("freshkart", cart["cart_id"])
    out = mcp.pay_quote("freshkart", quote["txn_ref"], quote["charge_amount_paise"] - 100)
    assert out.get("paid") is None
    assert out["code"] == "PRICE_CHANGED"          # the wallet's check, not ours
    assert FakeRazorpayClient.created == []


def test_only_pay_quote_touches_the_pay_endpoint(mcp):
    src = (REPO / "mcp_server" / "velcrow_mcp.py").read_text(encoding="utf-8")
    body_of_pay = src.split("def pay_quote")[1].split("@server.tool()")[0]
    everything_else = src.replace(body_of_pay, "")
    assert '/pay"' in body_of_pay
    assert '/pay"' not in everything_else          # no second tool can reach money


def test_the_mandate_caps_are_hard_coded_not_client_supplied(mcp):
    src = (REPO / "mcp_server" / "velcrow_mcp.py").read_text(encoding="utf-8")
    assert "MAX_TOTAL_PAISE = 500_000" in src
    assert "MAX_PER_TXN_PAISE = 300_000" in src
    # and no tool takes a cap as an argument
    assert "max_total" not in src.split("def list_shops")[1]
