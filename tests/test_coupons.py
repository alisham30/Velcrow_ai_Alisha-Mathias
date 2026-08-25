from __future__ import annotations

from shop.coupons import evaluate

GROCERY_COUPONS = [
    {"code": "FRESH50", "kind": "flat", "value_paise": 5000, "min_cart_paise": 39900,
     "stackable": True, "description": "Rs 50 off over Rs 399"},
    {"code": "FRESH150", "kind": "flat", "value_paise": 15000, "min_cart_paise": 140000,
     "stackable": False, "description": "Rs 150 off over Rs 1,400"},
    {"code": "STAPLES10", "kind": "percent", "value_pct": 10, "cap_paise": 12000,
     "min_cart_paise": 0, "category": "staples", "stackable": True,
     "description": "10% off staples up to Rs 120"},
]


def _line(item_id: str, qty: int, price: int, category: str) -> dict:
    return {"item_id": item_id, "variant": "", "qty": qty, "unit_price_paise": price,
            "category": category}


def test_no_coupon_below_minimum():
    r = evaluate([_line("lemons", 2, 4400, "produce")], GROCERY_COUPONS)  # 8800
    assert r["subtotal_paise"] == 8800
    assert r["applicable"] == []
    assert r["best"]["codes"] == [] and r["best"]["net_total_paise"] == 8800


def test_flat_coupon_applies_at_minimum():
    r = evaluate([_line("honey", 2, 21900, "packaged")], GROCERY_COUPONS)  # 43800 >= 39900
    codes = [a["code"] for a in r["applicable"]]
    assert "FRESH50" in codes and "FRESH150" not in codes
    assert r["best"]["discount_paise"] == 5000
    assert r["best"]["net_total_paise"] == 38800


def test_percent_coupon_respects_category_and_cap():
    r = evaluate([_line("basmati", 4, 42900, "staples")], GROCERY_COUPONS)  # staples 171600
    staples10 = next(a for a in r["applicable"] if a["code"] == "STAPLES10")
    assert staples10["discount_paise"] == 12000  # 10% = 17160, capped at 12000
    r2 = evaluate([_line("lemons", 10, 4400, "produce")], GROCERY_COUPONS)  # no staples in cart
    assert all(a["code"] != "STAPLES10" for a in r2["applicable"])


def test_stackable_coupons_combine_for_best():
    # staples 100000 -> STAPLES10 gives 10000; subtotal >= 39900 -> FRESH50 gives 5000
    r = evaluate([_line("atta", 4, 25000, "staples")], GROCERY_COUPONS)
    assert sorted(r["best"]["codes"]) == ["FRESH50", "STAPLES10"]
    assert r["best"]["discount_paise"] == 15000
    assert r["best"]["net_total_paise"] == 85000
    assert "FRESH50" in r["best"]["arithmetic"] and "STAPLES10" in r["best"]["arithmetic"]


def test_non_stackable_beats_combo_when_larger():
    # subtotal 145000 (staples): FRESH150 alone = 15000 vs FRESH50+STAPLES10 = 5000+12000(cap)=17000
    r = evaluate([_line("basmati", 5, 29000, "staples")], GROCERY_COUPONS)
    assert r["best"]["discount_paise"] == 17000
    # and with a non-staples cart of the same size, FRESH150 wins alone
    r2 = evaluate([_line("ghee", 5, 29000, "dairy")], GROCERY_COUPONS)
    assert r2["best"]["codes"] == ["FRESH150"]
    assert r2["best"]["net_total_paise"] == 130000


def test_near_miss_emitted_with_exact_math():
    # subtotal 130000 produce: best today = FRESH50 (net 125000).
    # FRESH150 minimum 140000, gap 10000 (= Rs 100 <= Rs 150 window):
    # net at 140000 - 15000 = 125000 -> NOT cheaper than 125000 -> no suggestion.
    r = evaluate([_line("lemons", 1, 130000, "produce")], GROCERY_COUPONS)
    assert r["near_miss"] is None
    # subtotal 132500: best = FRESH50 net 127500; gap 7500; 140000-15000=125000 < 127500 -> suggest
    r2 = evaluate([_line("lemons", 1, 132500, "produce")], GROCERY_COUPONS)
    nm = r2["near_miss"]
    assert nm is not None and nm["code"] == "FRESH150"
    assert nm["add_paise"] == 7500
    assert nm["unlocks_discount_paise"] == 15000
    assert nm["projected_net_paise"] == 125000
    assert nm["saves_paise"] == 2500
    assert "FRESH150" in nm["math"]


def test_near_miss_window_is_150_rupees():
    # subtotal 120000: gap to FRESH150 is 20000 paise (Rs 200) -> outside the window
    r = evaluate([_line("lemons", 1, 120000, "produce")], GROCERY_COUPONS)
    assert r["near_miss"] is None


def test_all_money_fields_are_integers():
    r = evaluate([_line("atta", 4, 25000, "staples")], GROCERY_COUPONS)
    assert isinstance(r["subtotal_paise"], int)
    assert isinstance(r["best"]["discount_paise"], int)
    assert isinstance(r["best"]["net_total_paise"], int)
    for a in r["applicable"]:
        assert isinstance(a["discount_paise"], int)
