"""Coupon optimizer (spec 4.3): enumerate applicable coupons, compute net
totals over allowed combinations, auto-apply the best, show the arithmetic.
Near-miss: if adding <= Rs 150 unlocks a coupon whose net total is lower,
emit a suggestion with the exact math. All money is integer paise.

Combination rule: a non-stackable coupon applies alone; any subset of
stackable coupons may combine (each discount computed on the cart subtotal).
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

NEAR_MISS_WINDOW_PAISE = 15000  # Rs 150


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    return f"{sign}Rs {paise // 100}.{paise % 100:02d}"


def _discount(coupon: dict[str, Any], subtotal: int, category_subtotals: dict[str, int]) -> int | None:
    """Discount in paise if the coupon applies at this subtotal, else None."""
    if subtotal < int(coupon.get("min_cart_paise", 0)):
        return None
    if coupon["kind"] == "flat":
        return min(int(coupon["value_paise"]), subtotal)
    if coupon["kind"] == "percent":
        base = category_subtotals.get(coupon["category"], 0) if coupon.get("category") else subtotal
        if base == 0:
            return None
        pct = (base * int(coupon["value_pct"])) // 100
        cap = int(coupon.get("cap_paise", pct))
        return min(pct, cap)
    raise ValueError(f"unknown coupon kind {coupon['kind']}")


def evaluate(lines: list[dict[str, Any]], coupons: list[dict[str, Any]]) -> dict[str, Any]:
    """`lines`: [{item_id, variant, qty, unit_price_paise, category}]."""
    subtotal = sum(int(l["qty"]) * int(l["unit_price_paise"]) for l in lines)
    cats: dict[str, int] = {}
    for l in lines:
        cats[l["category"]] = cats.get(l["category"], 0) + int(l["qty"]) * int(l["unit_price_paise"])

    applicable: list[dict[str, Any]] = []
    for c in coupons:
        d = _discount(c, subtotal, cats)
        if d is not None and d > 0:
            applicable.append({"code": c["code"], "discount_paise": d, "description": c["description"]})

    by_code = {c["code"]: c for c in coupons}
    candidates: list[list[dict[str, Any]]] = [[a] for a in applicable if not by_code[a["code"]].get("stackable")]
    stackable = [a for a in applicable if by_code[a["code"]].get("stackable")]
    for r in range(1, len(stackable) + 1):
        candidates.extend(list(combo) for combo in combinations(stackable, r))

    best: dict[str, Any] = {"codes": [], "discount_paise": 0, "net_total_paise": subtotal,
                            "arithmetic": f"subtotal {_rupees(subtotal)}, no coupon applicable"}
    for combo in candidates:
        discount = min(sum(a["discount_paise"] for a in combo), subtotal)
        net = subtotal - discount
        if net < best["net_total_paise"]:
            parts = " - ".join(f"{a['code']} {_rupees(a['discount_paise'])}" for a in combo)
            best = {"codes": [a["code"] for a in combo], "discount_paise": discount, "net_total_paise": net,
                    "arithmetic": f"subtotal {_rupees(subtotal)} - {parts} = {_rupees(net)}"}

    near_miss: dict[str, Any] | None = None
    for c in coupons:
        gap = int(c.get("min_cart_paise", 0)) - subtotal
        if not (0 < gap <= NEAR_MISS_WINDOW_PAISE):
            continue
        hyp_subtotal = subtotal + gap
        d = _discount(c, hyp_subtotal, cats)
        if d is None or d <= 0:
            continue
        hyp_net = hyp_subtotal - d
        if hyp_net < best["net_total_paise"]:
            saves = best["net_total_paise"] - hyp_net
            if near_miss is None or hyp_net < near_miss["projected_net_paise"]:
                near_miss = {
                    "code": c["code"], "add_paise": gap, "unlocks_discount_paise": d,
                    "projected_net_paise": hyp_net, "saves_paise": saves,
                    "math": (f"add {_rupees(gap)} -> unlock {c['code']} saving {_rupees(d)} "
                             f"-> net {_rupees(hyp_net)}, {_rupees(saves)} cheaper than today's best"),
                }

    return {"subtotal_paise": subtotal, "applicable": applicable, "best": best, "near_miss": near_miss}
