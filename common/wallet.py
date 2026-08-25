"""The only path to money (spec 5). Five ordered checks; every outcome is written to BOTH the
buyer chain and the shop chain. No other module may import the Razorpay SDK (test-enforced).
Payment recording: a real Razorpay TEST order is created and a simulated payment reference is
returned — test mode has no card UI in a server-side flow (Round-1 convention, user-visible)."""
from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import razorpay

from common import approval, chainlog, errors, mandate

SHOP_URLS: dict[str, str] = {"freshkart": "http://localhost:8001", "loomcraft": "http://localhost:8002"}


def _fetch_charge(shop_url: str, txn_ref: str) -> dict[str, Any]:
    resp = httpx.get(f"{shop_url}/order/{txn_ref}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _log_both(shop_id: str, event: str, why: str, data: dict[str, Any]) -> None:
    chainlog.append("buyer", event, why, data)
    chainlog.append(shop_id, event, why, data)


def pay(mandate_token: str, approval_token: str, shop_id: str, agreed_amount: int, txn_ref: str,
        shop_url: str | None = None) -> dict[str, Any]:
    """Run the five checks in order; money moves only if every one passes."""
    base: dict[str, Any] = {"shop_id": shop_id, "txn_ref": txn_ref, "amount_paise": agreed_amount}
    reserved_jti: str | None = None
    try:
        if not isinstance(agreed_amount, int) or agreed_amount <= 0:
            raise errors.OverCap("agreed_amount must be positive integer paise")
        claims: dict[str, Any] = mandate.verify(mandate_token)  # check 1: signature, expiry, revocation
        if shop_id not in claims["shops"]:  # check 2: shop allowed by mandate
            raise errors.ShopNotPermitted(f"mandate does not permit shop '{shop_id}'", shop_id=shop_id)
        if agreed_amount > claims["max_per_txn"]:  # check 3: per-txn cap + atomic total reserve
            raise errors.OverCap(f"{agreed_amount} paise exceeds max_per_txn {claims['max_per_txn']}",
                                 max_per_txn=claims["max_per_txn"], requested_paise=agreed_amount)
        mandate.reserve_spend(claims["jti"], agreed_amount, claims["max_total"])
        reserved_jti = claims["jti"]
        appr: dict[str, Any] = approval.verify(approval_token, merchant=shop_id, session_jti=claims["jti"])
        if appr["amount_paise"] != agreed_amount:  # check 4: cart-bound approval matches the charge
            raise errors.PriceChanged("approved amount differs from agreed amount",
                                      old_amount=appr["amount_paise"], new_amount=agreed_amount)
        charge = _fetch_charge(shop_url or SHOP_URLS[shop_id], txn_ref)
        if charge.get("status") != "pending":
            raise errors.IdempotentReplay(f"shop charge for {txn_ref} is '{charge.get('status')}', not pending")
        if charge["charge_amount"] != appr["amount_paise"]:
            raise errors.PriceChanged("shop charge amount differs from the approved amount",
                                      old_amount=appr["amount_paise"], new_amount=charge["charge_amount"])
        if approval.cart_hash(charge["line_items"]) != appr["cart_hash"]:
            raise errors.PriceChanged("cart changed between approval and charge: "
                                      + approval.diff_lines(appr["items"], charge["line_items"]))
        client = razorpay.Client(auth=(os.environ["RAZORPAY_KEY_ID"], os.environ["RAZORPAY_KEY_SECRET"]))
        payment_ref: str = f"pay_sim_{uuid.uuid4().hex[:12]}"  # check 5: create TEST order, record payment
        rzp_order: dict[str, Any] = client.order.create({
            "amount": agreed_amount, "currency": "INR", "receipt": txn_ref,
            "notes": {"shop_id": shop_id, "mandate_jti": claims["jti"], "payment_ref": payment_ref}})
    except Exception as e:
        if reserved_jti is not None:
            mandate.release_spend(reserved_jti, agreed_amount)
        info = e.payload() if isinstance(e, errors.VelcrowError) else {"code": "WALLET_ERROR"}
        _log_both(shop_id, "payment_refused", getattr(e, "why", str(e)), {**base, **info})
        raise
    result: dict[str, Any] = {**base, "razorpay_order_id": rzp_order["id"], "payment_ref": payment_ref,
                              "mandate_jti": claims["jti"]}
    _log_both(shop_id, "payment_created", f"all five wallet checks passed; Razorpay test order "
              f"{rzp_order['id']} created for {agreed_amount} paise at {shop_id}", result)
    return result
