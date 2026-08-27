"""The consumer buyer agent (spec 8): intent in, ranked options out.

Separate from the merchant widget in every sense. The widget works FOR a shop
inside that shop's page; this works FOR the shopper, across shops that do not
know about each other, and its loyalty is to the stated goal rather than to
either merchant.

The shape of a run:
    goal text -> rules (what they will and will not accept)
              -> mandate issued from those rules
              -> both shops queried through their PUBLIC agent surface
              -> options scored (spec 4.4) and rule-breakers greyed, not hidden
              -> human picks -> human approves -> wallet pays

Nothing here can move money. Selection and approval are separate human acts,
and payment still runs the wallet's five checks.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from common import chainlog, money, trust

# Scoring weights (spec 4.4). They sum to 1.0 and are stated here rather than
# buried, because a ranking nobody can inspect is a ranking nobody should trust.
W_PRICE, W_RULE_FIT, W_TRUST, W_AVAILABILITY = 0.4, 0.3, 0.2, 0.1

MAX_OPTIONS = 3


# -- goal -> rules ----------------------------------------------------------

_BUDGET = re.compile(
    r"(?:under|below|less than|upto|up to|max|within|budget(?:\s+of)?)\s*"
    r"(?:rs\.?|inr|₹)?\s*([\d,]+)", re.I)
_BARE_PRICE = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+)", re.I)
_SIZE = re.compile(r"\bsize\s+([a-z0-9]+)\b|\b(xs|s|m|l|xl|xxl)\b(?!\w)", re.I)
_EXACT = re.compile(r"\b(exact|exactly|only|same brand|brand only|no substitut\w*)\b", re.I)

_STOP = {
    "a", "an", "the", "under", "below", "less", "than", "upto", "up", "to", "max", "within",
    "budget", "of", "rs", "inr", "for", "me", "i", "want", "need", "buy", "get", "find",
    "please", "size", "exact", "exactly", "only", "brand", "no", "substitutes", "substitute",
    "cheapest", "best", "some", "any", "my", "and", "or", "with", "in", "at",
}


def parse_goal(text: str) -> dict[str, Any]:
    """Read a stated intent into rules the buyer can be held to.

    Deliberately deterministic: what the shopper is allowed to spend and what
    they refuse to accept are not things to ask a language model about. The
    model's job in this app is wording, never limits (spec 3).
    """
    raw = (text or "").strip()
    budget = _BUDGET.search(raw) or _BARE_PRICE.search(raw)
    budget_paise = int(budget.group(1).replace(",", "")) * 100 if budget else None

    size_match = _SIZE.search(raw)
    size = None
    if size_match:
        size = (size_match.group(1) or size_match.group(2) or "").upper()

    words = [w for w in re.findall(r"[a-zA-Z]+", raw) if w.lower() not in _STOP]
    if size and size.lower() in {w.lower() for w in words}:
        words = [w for w in words if w.lower() != size.lower()]

    return {
        "goal": raw,
        "query": " ".join(words[:4]),
        "budget_paise": budget_paise,
        "budget_display": money.rupees(budget_paise) if budget_paise else None,
        "size": size,
        "exact_only": bool(_EXACT.search(raw)),
    }


def missing_from(rules: dict[str, Any]) -> str | None:
    """What the buyer cannot proceed without. A missing budget is a normal
    clarification, never a red Blocked card (spec 8)."""
    if not rules["query"]:
        return "what you are looking for"
    if rules["budget_paise"] is None:
        return "a budget"
    return None


# -- discovery: both shops, through their public surface --------------------

def discover(shops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read each shop's manifest and catalog the way a stranger would.

    Uses only the published agent surface (spec 6.6), so nothing here depends
    on the two shops being ours.
    """
    found: list[dict[str, Any]] = []
    for shop in shops:
        try:
            with httpx.Client(base_url=shop["url"], timeout=10) as c:
                manifest = c.get("/.well-known/agent-commerce.json").json()
                catalog = c.get(manifest.get("catalog", "/agent/catalog")).json()
                caps = c.post("/agent/capabilities",
                              json={"capabilities": {}}).json()["capabilities"]
        except Exception as exc:
            found.append({"shop": shop, "error": f"{type(exc).__name__}: {exc}"})
            continue
        found.append({"shop": shop, "manifest": manifest, "catalog": catalog, "caps": caps})
    return found


def negotiate_and_buy(shop: dict[str, Any], item_id: str, variant: str, qty: int,
                      ceiling_paise: int, mandate_token: str,
                      contact: str = "") -> dict[str, Any]:
    """The buyer's half of a negotiation (phase 11). Deterministic, no LLM.

    Strategy, in full, because a negotiator whose policy is hidden cannot be
    audited by the shopper it spends for:

      open at 80% of the ceiling  - never reveal the ceiling first
      counter above the ceiling   - re-offer the ceiling, twice. The second
                                    identical offer tells the shop this is the
                                    end of the road, and its own deal-closing
                                    rule takes a floor-clearing final offer
                                    over a lost sale.
      counter within the ceiling  - take it. The token is signed and expiring;
                                    a better price is not worth losing this one.
      refusal naming a floor      - re-offer the floor if the ceiling covers
                                    it, else walk away. The shopper's ceiling
                                    is a hard line, exactly like the mandate
                                    caps: no deal is better than a bad deal.

    Every step lands on the buyer's own chain under the negotiation id, so the
    audit view can lay this record beside the shop's and show where - if
    anywhere - the two stories part.
    """
    story: list[dict[str, Any]] = []
    auth = {"Authorization": f"Mandate {mandate_token}"}
    # The buyer mints the negotiation id and the shop honours it, so the very
    # first chain entry - the opening - is already filed under the id both
    # sides will use. Logged before the id existed, the opening fell out of
    # the side-by-side record entirely.
    neg_id = "neg_" + uuid.uuid4().hex[:12]

    def tell(event: str, why: str, **data: Any) -> None:
        story.append({"event": event, "why": why, **data})
        chainlog.append("buyer", event, why, {"neg_id": neg_id, "shop_id": shop["shop_id"],
                                              "product_id": item_id, "variant": variant,
                                              "qty": qty, **data})

    def offer(paise: int) -> dict[str, Any]:
        with httpx.Client(base_url=shop["url"], timeout=10) as c:
            r = c.post("/negotiate", headers=auth,
                       json={"item_id": item_id, "variant": variant, "qty": qty,
                             "offer_paise": paise, **({"neg_id": neg_id} if neg_id else {})})
        r.raise_for_status()
        return r.json()

    def buy(token: dict[str, Any], unit_price: int) -> dict[str, Any]:
        with httpx.Client(base_url=shop["url"], timeout=10) as c:
            cart = c.post("/cart", json={}).json()["cart_id"]
            c.post(f"/cart/{cart}/fulfil", headers=auth,
                   json={"item_id": item_id, "variant": variant, "qty": qty, "mode": "add",
                         "contact_ref": contact})
            placed = c.post("/order", headers={**auth, "Idempotency-Key": f"neg-{neg_id}"},
                            json={"cart_id": cart, "offer_token": token, "assisted": True,
                                  "contact": contact})
            placed.raise_for_status()
            placed = placed.json()
            c.post("/confirm-payment",
                   json={"txn_ref": placed["txn_ref"],
                         "razorpay_order_id": f"order_neg_{neg_id}",
                         "payment_ref": f"pay_neg_{neg_id}"})
        tell("negotiated_purchase",
             f"bought {qty} x '{item_id}' at the negotiated {unit_price} paise/unit "
             f"({placed['charge_amount']} paise total), order {placed['txn_ref']}",
             txn_ref=placed["txn_ref"], amount_paise=placed["charge_amount"],
             unit_price_paise=unit_price)
        return placed

    opening = max(1, int(ceiling_paise * 0.8))
    price = opening
    stood_ground = 0        # ceiling offers made; two identical ones close a deal
    tell("negotiation_opened",
         f"opening at {opening} paise/unit for {qty} x '{item_id}' at {shop['shop_id']} "
         f"(shopper ceiling {ceiling_paise} paise/unit stays private)",
         offer_paise=opening, ceiling_paise=ceiling_paise)

    outcome: dict[str, Any] = {}
    for round_no in range(1, 6):
        answer = offer(price)
        decision = answer["decision"]
        if decision == "accepted":
            unit = int(answer["unit_price_paise"])
            tell("negotiation_agreed",
                 f"round {round_no}: shop accepted {unit} paise/unit - {answer['why']}",
                 unit_price_paise=unit, round=round_no)
            placed = buy(answer["offer_token"], unit)
            outcome = {"outcome": "bought", "unit_price_paise": unit,
                       "txn_ref": placed["txn_ref"], "charge_amount": placed["charge_amount"]}
            break
        if decision == "counter":
            counter = int(answer["unit_price_paise"])
            tell("negotiation_counter_received",
                 f"round {round_no}: shop countered at {counter} paise/unit "
                 f"(list {answer['list_paise']})", counter_paise=counter, round=round_no)
            if counter <= ceiling_paise:
                unit = counter
                tell("negotiation_agreed",
                     f"round {round_no}: counter {counter} is within the ceiling "
                     f"{ceiling_paise}; taking the signed offer", unit_price_paise=unit,
                     round=round_no)
                placed = buy(answer["offer_token"], unit)
                outcome = {"outcome": "bought", "unit_price_paise": unit,
                           "txn_ref": placed["txn_ref"],
                           "charge_amount": placed["charge_amount"]}
                break
            if stood_ground >= 2:
                # The ceiling was offered twice and the shop still wants more.
                tell("negotiation_walked",
                     f"round {round_no}: shop held at {counter}, above the ceiling "
                     f"{ceiling_paise}; walking away - no deal beats a bad deal",
                     counter_paise=counter)
                outcome = {"outcome": "walked", "shop_final_paise": counter}
                break
            price = ceiling_paise       # insist: the same number, twice, means it
            stood_ground += 1
            continue
        # refused
        floor = (answer.get("floor") or {}).get("minimum_unit_paise", 0)
        tell("negotiation_refused_by_shop",
             f"round {round_no}: refused - {answer['why']}", floor_paise=floor,
             round=round_no)
        if floor and floor <= ceiling_paise and price < floor:
            price = floor
            continue
        tell("negotiation_walked",
             f"round {round_no}: the floor ({floor}) is above the ceiling "
             f"({ceiling_paise}); walking away", floor_paise=floor)
        outcome = {"outcome": "walked", "shop_floor_paise": floor}
        break
    else:
        outcome = {"outcome": "walked", "why": "no agreement within four rounds"}

    return {"neg_id": neg_id, "shop_id": shop["shop_id"], "item_id": item_id,
            "variant": variant, "qty": qty, "ceiling_paise": ceiling_paise,
            "story": story, **outcome}


def _variant_for(product: dict[str, Any], size: str | None) -> tuple[str, int, str | None]:
    """(label, stock, restock_date) for the wanted size, or the best available."""
    variants = product.get("variants") or []
    if not variants:
        return "", int(product.get("stock", 0)), product.get("restock_date")
    if size:
        for v in variants:
            if v["label"].upper() == size.upper():
                return v["label"], int(v["stock"]), v.get("restock_date")
        return size, 0, None            # they asked for a size this shop does not carry
    best = max(variants, key=lambda v: v["stock"])
    return best["label"], int(best["stock"]), best.get("restock_date")


def _matches(product: dict[str, Any], query: str) -> int:
    haystack = " ".join([
        product.get("name", ""), product.get("description", ""),
        product.get("category", ""), " ".join(product.get("tags", [])),
    ]).lower()
    terms = [t for t in query.lower().split() if t]
    return sum(1 for t in terms if t in haystack or t.rstrip("s") in haystack)


def _candidates(catalog: list[dict[str, Any]], query: str) -> list[tuple[dict[str, Any], int]]:
    """Products worth showing for this query, strongest matches only.

    Every term has to match before a product counts. Without that, "cotton
    kurti" pulls in cotton socks, and because socks are cheap and price is the
    heaviest weight, the socks outrank the kurti - a ranking that is arithmetically
    correct and obviously useless. Partial matches are the fallback for when
    nothing matches fully, so a near miss still beats an empty page.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    scored = [(p, _matches(p, query)) for p in catalog]
    full = [(p, h) for p, h in scored if h == len(terms)]
    return full or [(p, h) for p, h in scored if h > 0]


def collect_options(discovered: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Every candidate from every shop, with the reasons it does or does not fit.

    A rule-breaker is kept, marked and explained - never dropped. The shopper
    decides what to do about a ₹1,600 kurti when they said ₹1,500; the agent
    does not get to hide it from them (spec 8).
    """
    options: list[dict[str, Any]] = []
    for entry in discovered:
        if "catalog" not in entry:
            continue
        shop = entry["shop"]
        terms = len([t for t in rules["query"].split() if t])
        for p, hits in _candidates(entry["catalog"], rules["query"]):
            label, stock, restock = _variant_for(p, rules["size"])
            price = int(p["price_paise"])

            breaks: list[str] = []
            if rules["budget_paise"] is not None and price > rules["budget_paise"]:
                over = price - rules["budget_paise"]
                breaks.append(f"{money.rupees(price)} is {money.rupees(over)} over your "
                              f"{rules['budget_display']} budget")
            if rules["size"] and (p.get("variants") and
                                  not any(v["label"].upper() == rules["size"].upper()
                                          for v in p["variants"])):
                breaks.append(f"this shop does not make it in size {rules['size']}")
            if rules["exact_only"] and hits < terms:
                breaks.append("you said exact only, and this matches part of what you asked for")

            options.append({
                "option_id": "opt_" + uuid.uuid4().hex[:10],
                "shop_id": shop["shop_id"], "shop_name": shop["name"], "shop_url": shop["url"],
                "item_id": p["id"], "name": p["name"],
                "category": p.get("category", ""),
                "variant": label,
                "price_paise": price, "price_display": money.rupees(price),
                "stock": stock, "restock_date": restock,
                "in_stock": stock > 0,
                "can_reserve": bool(entry["caps"].get("reservations")) and stock == 0,
                "match_strength": hits,
                "breaks_rules": breaks,
                "selectable": not breaks,
                "trust": trust.score(shop["shop_id"]),
            })
    return options


def rank(options: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Score every option and say, in words, why it placed where it did.

    score = 0.4*price + 0.3*rule_fit + 0.2*trust + 0.1*availability (spec 4.4).
    Rule-breakers are scored and kept so the shopper can see what they are
    turning down, but they sort last and can never be selected.
    """
    if not options:
        return []
    prices = [o["price_paise"] for o in options]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1

    for o in options:
        price_norm = 1.0 - ((o["price_paise"] - lo) / span)   # cheapest scores 1.0
        rule_fit = 0.0 if o["breaks_rules"] else min(1.0, 0.6 + 0.2 * o["match_strength"])
        availability = 1.0 if o["in_stock"] else (0.4 if o["can_reserve"] else 0.0)
        o["score"] = round(
            W_PRICE * price_norm + W_RULE_FIT * rule_fit
            + W_TRUST * o["trust"] + W_AVAILABILITY * availability, 4)
        o["score_parts"] = {
            "price": round(W_PRICE * price_norm, 3),
            "rule_fit": round(W_RULE_FIT * rule_fit, 3),
            "trust": round(W_TRUST * o["trust"], 3),
            "availability": round(W_AVAILABILITY * availability, 3),
        }
        # derived, not required as input: one source of truth for the amount
        o.setdefault("price_display", money.rupees(int(o["price_paise"])))
        bits = [f"{o['price_display']} at {o['shop_name']}"]
        if o["variant"]:
            bits.append(f"size {o['variant']}")
        if not o["in_stock"]:
            bits.append("out of stock — reservable" if o["can_reserve"] else "out of stock")
        bits.append(f"trust {o['trust']:.2f}")
        o["why"] = ", ".join(bits)

    options.sort(key=lambda o: (not o["selectable"], -o["score"]))
    return options


# -- run state, persisted (spec 8: refresh must restore the thread) ---------

RUNS_TABLE = "buyer_runs"


def _db():
    import os
    import sqlite3
    from pathlib import Path

    d = Path(os.environ.get("VELCROW_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "buyer.sqlite", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RUNS_TABLE} ("
        "  run_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_ts REAL NOT NULL,"
        "  updated_ts REAL NOT NULL)"
    )
    return conn


def save_run(state: dict[str, Any]) -> dict[str, Any]:
    """On disk, not in memory. A run the shopper can lose by refreshing is not
    a run - and the same mistake already cost this build its order history."""
    now = time.time()
    with _db() as conn:
        conn.execute(
            f"INSERT INTO {RUNS_TABLE} (run_id, state, created_ts, updated_ts)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET state = excluded.state,"
            "   updated_ts = excluded.updated_ts",
            (state["run_id"], json.dumps(state), now, now),
        )
    return state


def load_run(run_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute(
            f"SELECT state FROM {RUNS_TABLE} WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["state"]) if row else None


def new_run(goal: str) -> dict[str, Any]:
    return {
        "run_id": "buy_" + uuid.uuid4().hex[:12],
        "goal": goal,
        "created_ts": time.time(),
        "status": "new",
        "messages": [],
        "options": [],
        "rules": {},
        "mandate_token": None,
        "chosen": None,
        "quote": None,
        "receipt": None,
    }


def say(state: dict[str, Any], kind: str, text: str, **extra: Any) -> dict[str, Any]:
    """Append one card to the thread. `kind` decides how it reads: 'ask' is a
    normal clarification, 'blocked' is reserved for the four refusals in spec 8
    and must never be used for a missing budget."""
    state["messages"].append({"kind": kind, "text": text, "ts": time.time(), **extra})
    return state


def log_options(state: dict[str, Any]) -> None:
    top = state["options"][0] if state["options"] else None
    chainlog.append(
        "buyer", "buyer_options_ranked",
        f"goal {state['goal']!r}: {len(state['options'])} option(s) across shops; "
        + (f"best {top['name']} at {top['shop_name']} for {top['price_display']} "
           f"(score {top['score']})" if top else "nothing matched")
        + f"; {sum(1 for o in state['options'] if not o['selectable'])} shown but not selectable",
        {"run_id": state["run_id"], "goal": state["goal"],
         "options": [{"shop_id": o["shop_id"], "item_id": o["item_id"],
                      "price_paise": o["price_paise"], "score": o["score"],
                      "selectable": o["selectable"], "breaks_rules": o["breaks_rules"]}
                     for o in state["options"]]},
    )
