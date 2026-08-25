"""Tool implementations for the agent loop (spec 6.3). Zero LLM imports.

Every tool acts on the SAME cart the storefront is showing, through the
shop's own endpoints — there is no second cart inside the agent. The tool
layer, not the model, enforces the policies: quantity sanity and the
mandate's caps are re-checked in code here (spec 7.9).
"""
from __future__ import annotations

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


def _public_stock(p: dict[str, Any]) -> dict[str, Any]:
    if p.get("variants"):
        return {
            "variants": [
                {"label": v["label"], "stock": v["stock"], **({"restock_date": v["restock_date"]} if v.get("restock_date") else {})}
                for v in p["variants"]
            ]
        }
    return {"stock": p.get("stock", 0), **({"restock_date": p["restock_date"]} if p.get("restock_date") else {})}


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
    with _client(ctx["shop_url"]) as c:
        resp = c.patch(
            f"/cart/{ctx['cart_id']}",
            json={"op": "add", "item_id": item_id, "variant": variant or "", "qty": qty},
        )
    if resp.status_code != 200:
        raise _shop_error(resp)
    summary = _cart_summary(resp.json())
    summary["added"] = {"item_id": item_id, "variant": variant or "", "qty": qty}
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
    if resp.status_code != 200:
        raise _shop_error(resp)
    return _cart_summary(resp.json())


def remove_line(ctx: dict[str, Any], line_id: str, **_: Any) -> dict[str, Any]:
    with _client(ctx["shop_url"]) as c:
        resp = c.patch(f"/cart/{ctx['cart_id']}", json={"op": "remove", "line_id": line_id})
    if resp.status_code != 200:
        raise _shop_error(resp)
    return _cart_summary(resp.json())


def view_cart(ctx: dict[str, Any], **_: Any) -> dict[str, Any]:
    with _client(ctx["shop_url"]) as c:
        resp = c.get(f"/cart/{ctx['cart_id']}")
    if resp.status_code != 200:
        raise _shop_error(resp)
    return _cart_summary(resp.json())


REGISTRY = {
    "search_catalog": search_catalog,
    "add_to_cart": add_to_cart,
    "update_qty": update_qty,
    "remove_line": remove_line,
    "view_cart": view_cart,
}

MUTATING = {"add_to_cart", "update_qty", "remove_line"}


def summarise(name: str, result: dict[str, Any]) -> str:
    """One-line result summary for the SSE trace and the chain log."""
    if name == "search_catalog":
        n = result["match_count"]
        if n == 0:
            return "0 matches"
        top = result["matches"][0]
        return f"{n} match(es), best {top['name']} at {_rupees(top['price_paise'])}"
    if name in MUTATING or name == "view_cart":
        return f"cart {result['item_count']} item(s), {_rupees(result['subtotal_paise'])}"
    return "ok"
