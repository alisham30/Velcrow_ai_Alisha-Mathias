from __future__ import annotations

import pytest

from common import approval, errors, mandate

ITEMS = [
    {"item_id": "lemons-1kg", "variant": "", "qty": 2, "unit_price_paise": 4400},
    {"item_id": "peanut-butter-340g", "variant": "Crunchy", "qty": 1, "unit_price_paise": 24900},
]


def _jti() -> str:
    return mandate.verify(mandate.issue(500000, 500000, ["freshkart"]))["jti"]


def test_cart_hash_is_order_independent():
    assert approval.cart_hash(ITEMS) == approval.cart_hash(list(reversed(ITEMS)))


def test_cart_hash_changes_with_qty_price_or_line():
    h = approval.cart_hash(ITEMS)
    changed_qty = [dict(ITEMS[0], qty=3), ITEMS[1]]
    changed_price = [dict(ITEMS[0], unit_price_paise=4500), ITEMS[1]]
    removed = [ITEMS[0]]
    assert approval.cart_hash(changed_qty) != h
    assert approval.cart_hash(changed_price) != h
    assert approval.cart_hash(removed) != h


def test_issue_verify_roundtrip():
    jti = _jti()
    token = approval.issue("freshkart", "chk_1", ITEMS, 33700, jti)
    claims = approval.verify(token, merchant="freshkart", session_jti=jti)
    assert claims["amount_paise"] == 33700
    assert claims["cart_hash"] == approval.cart_hash(ITEMS)
    assert claims["currency"] == "INR"


def test_nonce_is_single_use():
    jti = _jti()
    token = approval.issue("freshkart", "chk_1", ITEMS, 33700, jti)
    approval.verify(token, merchant="freshkart", session_jti=jti)
    with pytest.raises(errors.MandateInvalid, match="nonce"):
        approval.verify(token, merchant="freshkart", session_jti=jti)


def test_bound_to_merchant():
    jti = _jti()
    token = approval.issue("freshkart", "chk_1", ITEMS, 33700, jti)
    with pytest.raises(errors.MandateInvalid, match="merchant"):
        approval.verify(token, merchant="loomcraft", session_jti=jti)


def test_bound_to_session_mandate():
    jti = _jti()
    token = approval.issue("freshkart", "chk_1", ITEMS, 33700, jti)
    with pytest.raises(errors.MandateInvalid, match="session"):
        approval.verify(token, merchant="freshkart", session_jti="some-other-jti")


def test_expired_approval_refused():
    jti = _jti()
    token = approval.issue("freshkart", "chk_1", ITEMS, 33700, jti, ttl_seconds=-5)
    with pytest.raises(errors.MandateExpired):
        approval.verify(token, merchant="freshkart", session_jti=jti)


def test_mandate_token_is_not_an_approval():
    m = mandate.issue(500000, 500000, ["freshkart"])
    jti = mandate.verify(m)["jti"]
    with pytest.raises(errors.MandateInvalid):
        approval.verify(m, merchant="freshkart", session_jti=jti)


def test_diff_lines_names_the_changed_line():
    charged = [dict(ITEMS[0], qty=5), ITEMS[1]]
    text = approval.diff_lines(ITEMS, charged)
    assert "lemons-1kg" in text and "qty=2" in text and "qty=5" in text
    added = ITEMS + [{"item_id": "honey-500g", "variant": "", "qty": 1, "unit_price_paise": 21900}]
    assert "line added: honey-500g" in approval.diff_lines(ITEMS, added)


def test_amount_must_be_positive_int():
    with pytest.raises(ValueError):
        approval.issue("freshkart", "chk_1", ITEMS, 0, "jti")
    with pytest.raises(ValueError):
        approval.issue("freshkart", "chk_1", ITEMS, 33700.0, "jti")  # type: ignore[arg-type]
