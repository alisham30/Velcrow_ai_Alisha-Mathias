"""Tool implementations for the agent loop (spec 6.3). Zero LLM imports.

Every tool acts on the SAME cart the storefront is showing, through the
shop's own endpoints — there is no second cart inside the agent. The tool
layer, not the model, enforces the policies: quantity sanity and the
mandate's caps are re-checked in code here (spec 7.9).
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from common import errors
from common.money import rupees as _rupees

MAX_QTY_PER_CALL = 20  # a tool-layer sanity bound, independent of the model


class ToolError(Exception):
    """A tool failed in a way the model should see and reason about."""

    def __init__(self, message: str, code: str = "TOOL_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _client(shop_url: str) -> httpx.Client:
    return httpx.Client(base_url=shop_url, timeout=15)


def _shop_error(resp: httpx.Response) -> ToolError:
    try:
        body = resp.json()
    except ValueError:
        return ToolError(f"shop returned HTTP {resp.status_code}", "SHOP_UNAVAILABLE")
    return ToolError(body.get("why") or f"HTTP {resp.status_code}", body.get("code", "TOOL_ERROR"))


def _cart_summary(cart: dict[str, Any]) -> dict[str, Any]:
    return {
        "cart_id": cart["cart_id"],
        "item_count": sum(l["qty"] for l in cart["items"]),
        "subtotal_paise": cart["subtotal_paise"],
        "subtotal_display": _rupees(cart["subtotal_paise"]),
        "lines": [
            {
                "line_id": l["line_id"],
                "item_id": l["item_id"],
                "name": l["name"],
                "variant": l["variant"],
                "qty": l["qty"],
                "unit_price_paise": l["unit_price_paise"],
                "unit_price_display": _rupees(l["unit_price_paise"]),
                "line_total_paise": l["qty"] * l["unit_price_paise"],
                "line_total_display": _rupees(l["qty"] * l["unit_price_paise"]),
            }
            for l in cart["items"]
        ],
    }


def _savings(ctx: dict[str, Any]) -> dict[str, Any]:
    """What this cart is currently leaving on the table.

    Attached to every cart result (spec 7.1 coupon rescue). The premise of the
    mechanic is that the shopper never thinks to ask, so the answer cannot wait
    to be asked for: the model gets it back automatically from the tool it just
    called. Policy in code, not in the prompt - the same rule the quantity and
    cap checks follow.

    A coupon lookup must never be able to break an add, so any failure here
    returns nothing and the cart operation stands.
    """
    try:
        with _client(ctx["shop_url"]) as c:
            resp = c.post(f"/cart/{ctx['cart_id']}/coupons", json={})
        if resp.status_code != 200:
            return {}
        result = resp.json()
    except Exception:
        return {}

    best = result["best"]
    out: dict[str, Any] = {
        "claimed": best["codes"],
        "discount_display": _rupees(best["discount_paise"]),
        "net_total_display": _rupees(best["net_total_paise"]),
    }
    if best["codes"]:
        out["tell_the_shopper"] = (
            f"{', '.join(best['codes'])} applied, saving {out['discount_display']} - "
            f"net {out['net_total_display']}"
        )
    near = result.get("near_miss")
    if near:
        out["near_miss"] = {
            "code": near["code"],
            "add_display": _rupees(near["add_paise"]),
            "unlocks_display": _rupees(near["unlocks_discount_paise"]),
            "projected_net_display": _rupees(near["projected_net_paise"]),
            "saves_display": _rupees(near["saves_paise"]),
            "math": near["math"],
        }
        out["tell_the_shopper"] = near["math"]
    return out


def _availability(stock: int) -> str:
    if stock <= 0:
        return "out of stock"
    return "only a few left" if stock <= 5 else "in stock"


def _public_stock(p: dict[str, Any]) -> dict[str, Any]:
    """What the model is told about supply - deliberately NOT a number.

    Given an exact count the model quietly lowers the shopper's request to
    match it ("add 16" became add_to_cart(qty=5) every time), and the shop
    then never learns the rest was wanted, so the shortfall is never reserved
    and the merchant loses that demand. How many can actually be supplied is
    the shop's decision, made in `fulfil`; the model's job is to pass on what
    the shopper asked for. The exact figures come back afterwards, from the
    shop, in the shortfall it reports.
    """
    if p.get("variants"):
        return {
            "variants": [
                {"label": v["label"], "availability": _availability(int(v["stock"])),
                 **({"restock_date": v["restock_date"]} if v.get("restock_date") else {})}
                for v in p["variants"]
            ]
        }
    return {"availability": _availability(int(p.get("stock", 0))),
            **({"restock_date": p["restock_date"]} if p.get("restock_date") else {})}


# -- tools ------------------------------------------------------------------

def _stem(word: str) -> str:
    """Crude singular form, enough that 'vegetables' matches the tag
    'vegetable' and 'lemons' matches 'lemon'."""
    for suffix in ("ies", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": p["id"],
        "name": p["name"],
        "category": p["category"],
        "tags": p.get("tags", []),
        "price_display": _rupees(p["price_paise"]),
        **_public_stock(p),
    }


def search_catalog(ctx: dict[str, Any], query: str, max_price_paise: int | None = None,
                   **_: Any) -> dict[str, Any]:
    with _client(ctx["shop_url"]) as c:
        resp = c.get("/catalog")
    if resp.status_code != 200:
        raise _shop_error(resp)
    products: list[dict[str, Any]] = resp.json()

    terms = [t for t in str(query).lower().split() if t]
    stems = [_stem(t) for t in terms]
    scored: list[tuple[int, dict[str, Any]]] = []
    for p in products:
        haystack = " ".join(
            [p["name"], p.get("description", ""), p.get("category", ""), " ".join(p.get("tags", []))]
        ).lower()
        stemmed = " ".join(_stem(w) for w in haystack.replace("(", " ").replace(")", " ").split())
        hits = sum(1 for t, s in zip(terms, stems) if t in haystack or s in stemmed)
        if terms and hits == 0:
            continue
        if max_price_paise is not None and p["price_paise"] > int(max_price_paise):
            continue
        scored.append((hits, p))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["price_paise"]))
    matches = [
        {
            "item_id": p["id"],
            "name": p["name"],
            "price_paise": p["price_paise"],
            "price_display": _rupees(p["price_paise"]),
            "category": p["category"],
            "exact_only": p.get("exact_only", False),
            **_public_stock(p),
        }
        for _, p in scored[:8]
    ]
    out: dict[str, Any] = {"query": query, "matches": matches, "match_count": len(matches)}
    if max_price_paise is not None:
        out["max_price_paise"] = int(max_price_paise)
        if not matches:
            out["note"] = (
                f"nothing at or below {max_price_paise} paise matched; "
                "tell the shopper rather than adding something dearer"
            )
    if not matches:
        # A word this shop does not use ("veggies", "sabzi") must never become
        # "we do not sell that". Hand back the whole shelf so the answer comes
        # from the catalog rather than from the failed query.
        out["catalog_overview"] = [_brief(p) for p in products[:40]]
        out["note"] = (
            out.get("note", "")
            + " The full catalog is in catalog_overview: answer from it, and do not tell the "
              "shopper the shop has none of something without checking it first."
        ).strip()
    return out


def add_to_cart(ctx: dict[str, Any], item_id: str, qty: int, variant: str | None = None,
                **_: Any) -> dict[str, Any]:
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise ToolError("qty must be a whole number", "BAD_QTY")
    if qty < 1:
        raise ToolError("qty must be at least 1", "BAD_QTY")
    if qty > MAX_QTY_PER_CALL:
        # policy lives in code, not in the prompt (spec 7.9)
        raise ToolError(
            f"refusing to add {qty} units in one step; the per-call limit is {MAX_QTY_PER_CALL}",
            "QTY_LIMIT",
        )
    # Take what the shelf allows and hold the rest, rather than refusing the
    # whole request (spec 7.2). Asking for 16 when 12 exist used to add
    # nothing, and the merchant lost all 16.
    with _client(ctx["shop_url"]) as c:
        resp = c.post(
            f"/cart/{ctx['cart_id']}/fulfil",
            json={"item_id": item_id, "variant": variant or "", "qty": qty,
                  "contact_ref": ctx.get("contact_ref") or "",
                  "shopper_ref": ctx.get("shopper_ref") or ""},
            headers={"Authorization": f"Mandate {ctx.get('mandate_token', '')}"},
        )
    if resp.status_code != 200:
        raise _shop_error(resp)
    result = resp.json()

    if not result["added"] and not result["reserved"]:
        # Nothing went in and nothing could be held: the shopper got nothing,
        # so this is a refusal however politely the shop phrased it.
        back = (f", back {result['restock_date']}" if result.get("restock_date") else "")
        raise ToolError(
            f"'{result['product_name']}'{' ' + variant if variant else ''} has "
            f"{result['in_stock_now']} in stock, {qty} requested{back}"
            + ("; this shop cannot hold any" if not result["can_reserve"] else ""),
            "OUT_OF_STOCK")

    summary = _cart_summary(result["cart"])
    summary["added"] = {"item_id": item_id, "variant": variant or "",
                        "qty": result["added"], "requested": qty}
    summary["savings"] = _savings(ctx)

    if result["shortfall"]:
        short = result["shortfall"]
        summary["shortfall"] = {
            "requested": qty, "added": result["added"], "short_by": short,
            "reserved": result["reserved"],
            "restock_date": result.get("restock_date"),
            "value_display": _rupees(short * int(result["unit_price_paise"])),
        }
        # The wording has to match which of these actually happened. "only 0
        # were in stock, so I added those" is nonsense, and reads as broken.
        already = int(result.get("already_in_cart", 0))
        outstanding = int(result.get("outstanding", qty))
        if result["added"] and already:
            got = (f"you already had {already}, and only {result['added']} more were in stock, "
                   "so I added those")
        elif result["added"]:
            got = f"only {result['added']} of the {qty} were in stock, so I added those"
        elif already:
            got = (f"your basket already holds all {already} they had, so none of the "
                   f"{outstanding} you still wanted could be added")
        else:
            got = f"there were none left of the {qty} you asked for"

        if result["reserved"]:
            summary["shortfall"]["res_id"] = result["reservation"]["res_id"]
            summary["tell_the_shopper"] = (
                f"{got}, and I reserved {'the other ' if result['added'] else 'all '}{short}"
                + (f" — back {result['restock_date']}" if result.get("restock_date") else "")
                + ". They will be offered to you the moment they land, and nothing is charged "
                  "for them now.")
        else:
            summary["tell_the_shopper"] = (
                f"{got}. This shop does not take reservations, so "
                f"{'the other ' if result['added'] else ''}{short} cannot be held.")
    return summary


def update_qty(ctx: dict[str, Any], line_id: str, qty: int, **_: Any) -> dict[str, Any]:
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise ToolError("qty must be a whole number", "BAD_QTY")
    if qty < 0:
        raise ToolError("qty cannot be negative", "BAD_QTY")
    if qty > MAX_QTY_PER_CALL:
        raise ToolError(
            f"refusing to set quantity to {qty}; the per-call limit is {MAX_QTY_PER_CALL}",
            "QTY_LIMIT",
        )
    with _client(ctx["shop_url"]) as c:
        resp = c.patch(f"/cart/{ctx['cart_id']}", json={"op": "update", "line_id": line_id, "qty": qty})
        if resp.status_code == 409 or (resp.status_code >= 400
                                       and resp.json().get("code") == "OUT_OF_STOCK"):
            # Same rule as adding: take what the shelf allows and hold the
            # rest, rather than dead-ending the shopper (spec 7.2). Without
            # this, "make that 10" when 5 exist simply failed.
            line = next((l for l in c.get(f"/cart/{ctx['cart_id']}").json()["items"]
                         if l["line_id"] == line_id), None)
            if line is None:
                raise _shop_error(resp)
            extra = qty - int(line["qty"])
            if extra > 0:
                return add_to_cart(ctx, item_id=line["item_id"], qty=extra,
                                   variant=line["variant"] or None)
    if resp.status_code != 200:
        raise _shop_error(resp)
    return {**_cart_summary(resp.json()), "savings": _savings(ctx)}


def remove_line(ctx: dict[str, Any], line_id: str, **_: Any) -> dict[str, Any]:
    with _client(ctx["shop_url"]) as c:
        resp = c.patch(f"/cart/{ctx['cart_id']}", json={"op": "remove", "line_id": line_id})
    if resp.status_code != 200:
        raise _shop_error(resp)
    return {**_cart_summary(resp.json()), "savings": _savings(ctx)}


def view_cart(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    with _client(ctx["shop_url"]) as c:
        resp = c.get(f"/cart/{ctx['cart_id']}")
    if resp.status_code != 200:
        raise _shop_error(resp)
    return {**_cart_summary(resp.json()), "savings": _savings(ctx)}


# -- Phase 5: coupons, reorder, conversational checkout ---------------------


def apply_best_coupons(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Claim the best coupon set for this cart and show the arithmetic.

    Nothing is "applied" to a stored cart: the shop recomputes and applies the
    best set at order time, so this reports today's answer rather than pinning
    a stale one. It also returns what was NOT claimed and the near-miss math,
    because the missed saving is half the point (spec 7.1).
    """
    with _client(ctx["shop_url"]) as c:
        resp = c.post(f"/cart/{ctx['cart_id']}/coupons", json={})
    if resp.status_code != 200:
        raise _shop_error(resp)
    result = resp.json()

    best = result["best"]
    out: dict[str, Any] = {
        "subtotal_paise": result["subtotal_paise"],
        "subtotal_display": _rupees(result["subtotal_paise"]),
        "claimed": best["codes"],
        "discount_paise": best["discount_paise"],
        "discount_display": _rupees(best["discount_paise"]),
        "net_total_paise": best["net_total_paise"],
        "net_total_display": _rupees(best["net_total_paise"]),
        "arithmetic": best["arithmetic"],
        "applicable": [
            {**a, "discount_display": _rupees(a["discount_paise"])}
            for a in result["applicable"]
        ],
        "not_claimed": [
            {**a, "discount_display": _rupees(a["discount_paise"])}
            for a in result["applicable"] if a["code"] not in best["codes"]
        ],
    }
    near = result.get("near_miss")
    if near:
        out["near_miss"] = {
            **near,
            "add_display": _rupees(near["add_paise"]),
            "unlocks_display": _rupees(near["unlocks_discount_paise"]),
            "projected_net_display": _rupees(near["projected_net_paise"]),
            "saves_display": _rupees(near["saves_paise"]),
        }
    return out


def reorder_last(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Re-quote the shopper's last completed basket at today's prices.

    Read-only on purpose (spec 6.3: "shows price deltas, one confirm"). It
    reports what the basket costs now and what moved; the shopper confirms
    before anything is added, and the model then adds the lines.
    """
    ref = str(ctx.get("shopper_ref") or "")
    key = str(ctx.get("contact_key") or "")
    if not ref and not key:
        raise ToolError(
            "there is no shopper key on this session, so there is no past order to look up",
            "NO_ORDER_HISTORY")
    with _client(ctx["shop_url"]) as c:
        resp = c.get("/orders/last", params={"shopper_ref": ref, "contact_key": key})
    if resp.status_code == 404:
        raise ToolError(
            "this shopper has no completed order at this shop yet, so there is nothing to reorder. "
            + ("If they bought on another device, ask for the phone or email they used and call "
               "identify_shopper." if not key else "Nothing is stored under their contact either."),
            "NO_ORDER_HISTORY")
    if resp.status_code != 200:
        raise _shop_error(resp)
    past = resp.json()

    lines = []
    for l in past["lines"]:
        entry = {
            "item_id": l["item_id"], "name": l["name"], "variant": l.get("variant", ""),
            "qty": l["qty"], "available": l.get("available", False),
        }
        if "now_unit_price_paise" in l:
            delta = int(l["delta_paise"])
            entry.update({
                "then_price_display": _rupees(int(l["then_unit_price_paise"])),
                "now_price_display": _rupees(int(l["now_unit_price_paise"])),
                "line_total_display": _rupees(int(l["now_line_total_paise"])),
                "price_moved": delta != 0,
                "delta_display": ("unchanged" if delta == 0
                                  else f"{'up' if delta > 0 else 'down'} {_rupees(abs(delta))}"),
            })
        if not entry["available"]:
            entry["unavailable_reason"] = l.get("unavailable_reason", "unavailable")
        lines.append(entry)

    delta = int(past["delta_paise"])
    moved = [l for l in lines if l.get("price_moved")]
    missing = [l for l in lines if not l["available"]]

    # The deltas are the point of a re-quote (spec 12 acceptance: "re-quotes
    # with deltas"). A shopper agreeing to "the usual" is agreeing to a price,
    # so a movement they were not told about is a bait-and-switch. The changed
    # lines are therefore lifted out as ready-made sentences with an explicit
    # instruction attached, not left for the model to notice in a list.
    note = "Report these lines and the new subtotal. "
    if moved:
        note += ("PRICES HAVE CHANGED since they last bought this. You MUST say which "
                 "items moved and by how much, quoting price_changes, before you ask them "
                 "to confirm. Do not present this basket as unchanged. ")
    if missing:
        note += "Name every line in unavailable and why; never drop one silently. "
    note += ("Then ask for one confirmation. Do not add anything in the same turn as "
             "the quote.")

    return {
        "ordered_at": past["ordered_at"],
        "lines": lines,
        "then_subtotal_display": _rupees(int(past["then_subtotal_paise"])),
        "now_subtotal_display": _rupees(int(past["now_subtotal_paise"])),
        "total_delta_display": ("the same as last time" if delta == 0
                                else f"{'up' if delta > 0 else 'down'} {_rupees(abs(delta))}"),
        "any_price_changed": bool(moved),
        "price_changes": [
            f"{l['name']}: was {l['then_price_display']} each, now {l['now_price_display']} "
            f"each ({l['delta_display']})"
            for l in moved
        ],
        "unavailable": [f"{l['name']}: {l.get('unavailable_reason', 'unavailable')}"
                        for l in missing],
        "unavailable_count": len(missing),
        "note": note,
    }


def identify_shopper(ctx: dict[str, Any], contact: str, **_: Any) -> dict[str, Any]:
    """Bind this browser to a contact the shopper gives, so their history
    follows them rather than their device (spec 7.3).

    Mutates ctx so the rest of THIS turn already knows the key - the shopper
    says "it was 9821158848" and the very next reorder_last finds the basket,
    without costing them another turn.

    Not authentication. A contact is a claim, not a proof, and it unlocks
    history only: money still needs a mandate, an approval and the wallet.
    """
    with _client(ctx["shop_url"]) as c:
        resp = c.post("/shopper/identify",
                      json={"contact": contact, "shopper_ref": ctx.get("shopper_ref", "")})
    if resp.status_code != 200:
        raise _shop_error(resp)
    result = resp.json()
    ctx["contact_key"] = result["contact_key"]
    return {
        "contact": result["contact_ref"],
        "linked_devices": result["linked_devices"],
        "orders_claimed": result["orders_claimed"],
        "has_history": result["has_history"],
        "note": ("Remembered. Call reorder_last now if they asked about a past order. "
                 if result["has_history"] else
                 "Nothing has ever been bought under that contact at this shop; say so plainly "
                 "rather than implying an order exists."),
    }


def start_checkout(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Turn the cart into a priced, stock-held quote the human can approve.

    This tool CANNOT pay, and there is no tool that can. It creates the order
    at the shop and hands back exactly what the approval card must show. The
    money then moves only when the human taps Approve, which is what signs the
    cart-bound approval (spec 5.1) and runs the wallet's five checks.
    """
    token = ctx.get("mandate_token")
    if not token:
        raise ToolError("no session mandate is present, so no checkout can be quoted",
                        "MANDATE_INVALID")
    with _client(ctx["shop_url"]) as c:
        resp = c.post("/order",
                      json={"cart_id": ctx["cart_id"],
                            "shopper_ref": ctx.get("shopper_ref") or "",
                            "contact": ctx.get("contact_ref") or "",
                            # the agent drove this checkout, not the storefront
                            # form - this is what the console's assisted vs
                            # unassisted split is counted from (spec 6.1, 7.4)
                            "assisted": True},
                      headers={"Authorization": f"Mandate {token}",
                               "Idempotency-Key": f"chk-{ctx['cart_id']}-{ctx.get('turn_id', '')}"})
    if resp.status_code not in (200, 201):
        raise _shop_error(resp)
    order = resp.json()

    with _client(ctx["shop_url"]) as c:
        cart = c.get(f"/cart/{ctx['cart_id']}").json()
    names = {(l["item_id"], l["variant"]): l["name"] for l in cart["items"]}

    coupon = order.get("coupon") or {}
    return {
        "txn_ref": order["txn_ref"],
        "shop_id": order["shop_id"],
        "charge_amount_paise": order["charge_amount"],
        "charge_display": _rupees(int(order["charge_amount"])),
        "coupon_codes": coupon.get("codes") or [],
        "coupon_discount_display": _rupees(int(coupon.get("discount_paise", 0))),
        "coupon_arithmetic": coupon.get("arithmetic", ""),
        "line_items": [
            {**li,
             "name": names.get((li["item_id"], li["variant"]), li["item_id"]),
             "unit_price_display": _rupees(int(li["unit_price_paise"])),
             "line_total_display": _rupees(int(li["unit_price_paise"]) * int(li["qty"]))}
            for li in order["line_items"]
        ],
        "expires_at": order.get("expires_at"),
        "awaiting_human_approval": True,
        "note": ("A quote only. The shopper now sees an approval card and must tap Approve "
                 "before any money moves. Tell them the total and that you are waiting on "
                 "their approval. Never claim the order is paid."),
    }


REGISTRY = {
    "search_catalog": search_catalog,
    "add_to_cart": add_to_cart,
    "update_qty": update_qty,
    "remove_line": remove_line,
    "view_cart": view_cart,
    "apply_best_coupons": apply_best_coupons,
    "reorder_last": reorder_last,
    "identify_shopper": identify_shopper,
    "start_checkout": start_checkout,
}

MUTATING = {"add_to_cart", "update_qty", "remove_line"}


def summarise(name: str, result: dict[str, Any]) -> str:
    """One-line result summary for the SSE trace and the chain log."""
    if name == "search_catalog":
        n = result.get("match_count", 0)
        matches = result.get("matches") or []
        if n == 0 or not matches:
            return "0 matches"
        top = matches[0]
        return f"{n} match(es), best {top['name']} at {_rupees(top['price_paise'])}"
    if name in MUTATING or name == "view_cart":
        line = (f"cart {result.get('item_count', 0)} item(s), "
                f"{_rupees(int(result.get('subtotal_paise', 0)))}")
        short = result.get("shortfall")
        if short:
            line += (f", {short['added']}/{short['requested']} in stock"
                     + (f", {short['reserved']} reserved" if short["reserved"]
                        else f", {short['short_by']} unavailable"))
        # the strip should show the agent noticing, not just the cart moving
        savings = result.get("savings") or {}
        if savings.get("claimed"):
            line += f", {', '.join(savings['claimed'])} -{savings['discount_display']}"
        elif savings.get("near_miss"):
            line += f", near-miss {savings['near_miss']['code']}"
        return line
    if name == "apply_best_coupons":
        claimed = result.get("claimed") or []
        head = (f"{', '.join(claimed)} -{result['discount_display']}" if claimed
                else "no coupon applies")
        near = result.get("near_miss")
        tail = f", near-miss {near['code']}" if near else ""
        return f"{head}, net {result['net_total_display']}{tail}"
    if name == "reorder_last":
        n = len(result.get("lines") or [])
        return (f"last basket: {n} line(s), {result['now_subtotal_display']}, "
                f"{result['total_delta_display']}")
    if name == "identify_shopper":
        return (f"{result['contact']} -> {result['linked_devices']} device(s), "
                f"{result['orders_claimed']} order(s) claimed, "
                f"{'history found' if result['has_history'] else 'no history'}")
    if name == "start_checkout":
        return (f"quote {result['txn_ref']} for {result['charge_display']}, "
                "awaiting the shopper's approval")
    return "ok"


def call_display(name: str, args: dict[str, Any]) -> str:
    """The exact call the model made, as one readable line:
    `search_catalog(query="lemons", max_price_paise=10000)`.

    Rendered here rather than in the widget for the same reason money is
    (spec 6.5, and the money-formatting fix in BREAKAGE.md): the reasoning
    strip is evidence that the agent chose its own actions, so the string it
    shows is built once, server-side, where a test can assert it is complete.
    """
    if not name:
        return "(unnamed tool)"
    rendered = ", ".join(
        f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in (args or {}).items()
    )
    return f"{name}({rendered})"
