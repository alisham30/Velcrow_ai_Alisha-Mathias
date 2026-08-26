from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Isolated data dir + deterministic secrets for every test."""
    monkeypatch.setenv("VELCROW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MANDATE_SECRET", "test-secret-" + "0" * 32)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_dummy")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "dummy-secret")

    # Point the shop's restock callback at a dead port. Otherwise a test run on
    # a machine where .\run_all.ps1 is up reaches the REAL agent on 8003, and
    # tests that expect a refusal to stay outstanding see it notified instead -
    # the same test passing or failing depending on whether the dev services
    # happen to be running. Tests that want the callback stub httpx themselves.
    monkeypatch.setenv("VELCROW_AGENT_URL", "http://127.0.0.1:9")
    import shop.app
    monkeypatch.setattr(shop.app, "AGENT_URL", "http://127.0.0.1:9", raising=False)
    return monkeypatch


def _make_client(monkeypatch, shop_kind: str) -> TestClient:
    from shop.app import create_app

    monkeypatch.setenv("SHOP", shop_kind)
    return TestClient(create_app(), raise_server_exceptions=True)


@pytest.fixture
def freshkart(env) -> TestClient:
    return _make_client(env, "grocery")


@pytest.fixture
def loomcraft(env) -> TestClient:
    return _make_client(env, "apparel")


@pytest.fixture
def buyer_mandate():
    """A generous mandate valid at both shops: Rs 100,000 total, Rs 50,000/txn."""
    from common import mandate

    token = mandate.issue(
        max_total=10_000_000, max_per_txn=5_000_000, shops=["freshkart", "loomcraft"], ttl_seconds=600
    )
    return token
