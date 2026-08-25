"""Typed error taxonomy (spec 6.6). Every failure carries a machine code,
the actions available to the caller, and a human-readable `why`.

Codes are the fixed set from the spec, plus NOT_OUT_OF_STOCK (judgment call:
/reserve on an in-stock item needed a truthful answer and no spec code fits).
"""
from __future__ import annotations

from typing import Any


class VelcrowError(Exception):
    code: str = "VELCROW_ERROR"
    http_status: int = 400
    default_actions: list[str] = []

    def __init__(self, why: str, available_actions: list[str] | None = None, **fields: Any) -> None:
        super().__init__(why)
        self.why = why
        self.fields = fields
        self.available_actions = (
            available_actions if available_actions is not None else list(self.default_actions)
        )

    def payload(self) -> dict[str, Any]:
        # extra fields must never shadow the taxonomy keys
        return {
            **self.fields,
            "code": self.code,
            "available_actions": self.available_actions,
            "why": self.why,
        }


class OutOfStock(VelcrowError):
    code = "OUT_OF_STOCK"
    http_status = 409
    default_actions = ["RESERVE", "SELECT_ALTERNATIVE"]


class PriceChanged(VelcrowError):
    code = "PRICE_CHANGED"
    http_status = 409
    default_actions = ["REQUOTE", "CANCEL"]

    def payload(self) -> dict[str, Any]:
        out = super().payload()
        out.setdefault("requires_reapproval", True)
        return out


class MandateInvalid(VelcrowError):
    code = "MANDATE_INVALID"
    http_status = 402
    default_actions = ["OBTAIN_MANDATE"]


class MandateExpired(VelcrowError):
    code = "MANDATE_EXPIRED"
    http_status = 402
    default_actions = ["OBTAIN_MANDATE"]


class OverCap(VelcrowError):
    code = "OVER_CAP"
    http_status = 402
    default_actions = ["REDUCE_AMOUNT", "OBTAIN_MANDATE"]


class ShopNotPermitted(VelcrowError):
    code = "SHOP_NOT_PERMITTED"
    http_status = 403
    default_actions = ["OBTAIN_MANDATE"]


class CouponIneligible(VelcrowError):
    code = "COUPON_INELIGIBLE"
    http_status = 422
    default_actions = ["VIEW_COUPONS"]


class IdempotentReplay(VelcrowError):
    code = "IDEMPOTENT_REPLAY"
    http_status = 409
    default_actions = ["USE_NEW_KEY"]


class CapabilityUnsupported(VelcrowError):
    code = "CAPABILITY_UNSUPPORTED"
    http_status = 400
    default_actions = ["NEGOTIATE_CAPABILITIES"]


class NotOutOfStock(VelcrowError):
    code = "NOT_OUT_OF_STOCK"
    http_status = 409
    default_actions = ["ADD_TO_CART"]


# Infrastructure codes — not part of the spec's commerce taxonomy; used for
# plain missing-resource / malformed-request failures so commerce codes are
# never abused for them.
class NotFound(VelcrowError):
    code = "NOT_FOUND"
    http_status = 404


class BadRequest(VelcrowError):
    code = "BAD_REQUEST"
    http_status = 422
