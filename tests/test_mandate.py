from __future__ import annotations

import pytest

from common import approval, errors, mandate


def test_issue_verify_roundtrip():
    token = mandate.issue(200000, 100000, ["freshkart"], ttl_seconds=60)
    claims = mandate.verify(token)
    assert claims["typ"] == "mandate"
    assert claims["max_total"] == 200000
    assert claims["max_per_txn"] == 100000
    assert claims["shops"] == ["freshkart"]
    assert claims["rules"]["payment_pref"] == "razorpay_test"
    assert claims["jti"]


def test_expired_mandate_refused():
    token = mandate.issue(200000, 100000, ["freshkart"], ttl_seconds=-5)
    with pytest.raises(errors.MandateExpired):
        mandate.verify(token)


def test_forged_signature_refused():
    token = mandate.issue(200000, 100000, ["freshkart"])
    header, payload, sig = token.split(".")
    forged = f"{header}.{payload}.{'A' * len(sig)}"
    with pytest.raises(errors.MandateInvalid):
        mandate.verify(forged)


def test_token_signed_with_wrong_secret_refused(monkeypatch):
    monkeypatch.setenv("MANDATE_SECRET", "attacker-secret-xxxxxxxxxxxxxxxxxxxx")
    token = mandate.issue(200000, 100000, ["freshkart"])
    monkeypatch.setenv("MANDATE_SECRET", "test-secret-" + "0" * 32)
    with pytest.raises(errors.MandateInvalid):
        mandate.verify(token)


def test_approval_token_is_not_a_mandate():
    m = mandate.issue(200000, 100000, ["freshkart"])
    jti = mandate.verify(m)["jti"]
    appr = approval.issue("freshkart", "chk_1", [
        {"item_id": "x", "variant": "", "qty": 1, "unit_price_paise": 100}], 100, jti)
    with pytest.raises(errors.MandateInvalid):
        mandate.verify(appr)


def test_revoked_mandate_refused():
    token = mandate.issue(200000, 100000, ["freshkart"])
    jti = mandate.verify(token)["jti"]
    mandate.revoke(jti)
    with pytest.raises(errors.MandateInvalid):
        mandate.verify(token)


def test_caps_must_be_integer_paise():
    with pytest.raises(TypeError):
        mandate.issue(2000.50, 1000, ["freshkart"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mandate.issue(1000, 2000, ["freshkart"])  # per-txn above total
    with pytest.raises(ValueError):
        mandate.issue(0, 0, ["freshkart"])


def test_reserve_spend_enforces_max_total():
    token = mandate.issue(100000, 100000, ["freshkart"])
    jti = mandate.verify(token)["jti"]
    mandate.reserve_spend(jti, 60000, 100000)
    with pytest.raises(errors.OverCap):
        mandate.reserve_spend(jti, 50000, 100000)  # 60000 + 50000 > 100000
    mandate.reserve_spend(jti, 40000, 100000)  # exactly at cap is fine
    assert mandate.spent(jti) == 100000


def test_release_spend_frees_budget():
    token = mandate.issue(100000, 100000, ["freshkart"])
    jti = mandate.verify(token)["jti"]
    mandate.reserve_spend(jti, 100000, 100000)
    mandate.release_spend(jti, 100000)
    assert mandate.spent(jti) == 0
    mandate.reserve_spend(jti, 100000, 100000)  # budget available again


def test_failed_reserve_reserves_nothing():
    token = mandate.issue(50000, 50000, ["freshkart"])
    jti = mandate.verify(token)["jti"]
    with pytest.raises(errors.OverCap):
        mandate.reserve_spend(jti, 60000, 50000)
    assert mandate.spent(jti) == 0
