"""The ONLY module in the codebase that imports the LLM SDK (spec 3).

The agent is a tool-calling loop (spec 0.8, 6.3): the model is handed tool
definitions and chooses among them. There is no keyword matching and no
intent classifier on the live path.

`deterministic_fallback` exists solely so a dead API degrades instead of
killing a demo (spec 3). It is NOT the agent — anything it produces is
marked degraded so the UI can say so out loud.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import openai

from common.money import rupees

MODEL = "gpt-4o-mini"
MAX_ROUNDS = 6

_REASON = {
    "type": "string",
    "description": "One short sentence: why you are calling this tool right now. Shown to the shopper.",
}

# Phase 4 wires the cart tools only. Coupons, reserve, reorder and checkout
# arrive in Phase 5 — a tool the shop cannot honour yet is worse than no tool.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": (
                "Search this shop's catalog. Use this before adding anything — never guess a "
                "product id. Returns ids, prices and live stock. If nothing matches, the "
                "result also carries catalog_overview: the whole shelf, so you can still "
                "answer what the shop actually sells."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to match, e.g. 'lemons'"},
                    "max_price_paise": {
                        "type": "integer",
                        "description": "Optional unit-price ceiling in integer paise (Rs 100 = 10000)",
                    },
                    "reason": _REASON,
                },
                "required": ["query", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Add a quantity of one product to the shopper's cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "description": "Exact id from search_catalog"},
                    "variant": {
                        "type": "string",
                        "description": "Size or pack label when the product has variants, else omit",
                    },
                    "qty": {"type": "integer", "description": "Whole units, at least 1"},
                    "reason": _REASON,
                },
                "required": ["item_id", "qty", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_qty",
            "description": "Change the quantity of a line already in the cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string", "description": "line_id from view_cart"},
                    "qty": {"type": "integer", "description": "New quantity; 0 removes the line"},
                    "reason": _REASON,
                },
                "required": ["line_id", "qty", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_line",
            "description": "Remove a line from the cart entirely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string", "description": "line_id from view_cart"},
                    "reason": _REASON,
                },
                "required": ["line_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_cart",
            "description": "Read the shopper's current cart with line ids, quantities and totals.",
            "parameters": {
                "type": "object",
                "properties": {"reason": _REASON},
                "required": ["reason"],
            },
        },
    },
]


def system_prompt(context: dict[str, Any]) -> str:
    """Live context for this turn: which shop, what is in the cart, what the
    mandate allows (spec 6.3). The catalog itself is reached through
    search_catalog so the agent works the same at a shop of any size."""
    cart_lines = context.get("cart_lines") or []
    if cart_lines:
        cart_text = "\n".join(
            f"  - line_id={l['line_id']} {l['name']}"
            + (f" [{l['variant']}]" if l.get("variant") else "")
            + f" qty={l['qty']} each {rupees(l['unit_price_paise'])}"
            + f" line total {rupees(l['qty'] * l['unit_price_paise'])}"
            for l in cart_lines
        )
    else:
        cart_text = "  (empty)"

    return f"""You are VelcrowAI, a shopping agent installed inside {context['shop_name']},
a {context['shop_category']} shop. You help this shopper build their cart.

LIVE CONTEXT FOR THIS TURN
Shop: {context['shop_name']} ({context['shop_id']})
Categories sold: {", ".join(context['categories'])}
Products in catalog: {context['catalog_size']}
Variants are called: {context['variant_kind']}
Cart ({context['cart_count']} item(s), subtotal {rupees(context['subtotal_paise'])}):
{cart_text}
Spending limits on this session: at most {rupees(context['max_per_txn_paise'])} per payment,
{rupees(context['max_total_paise'])} in total.

RULES
- NEVER do arithmetic on money and never convert paise to rupees yourself.
  Every tool result carries ready-made strings (price_display, line_total_display,
  subtotal_display) already written in rupees. Quote those verbatim. If you need
  an amount you were not given a display string for, call view_cart and use its
  subtotal_display rather than working it out.
- Tool INPUTS are integer paise: Rs 100 is 10000 paise. Never send decimals.
- Never say the shop does not stock something on the strength of one empty
  search. The shopper's word may not be the shop's word ("veggies" vs the
  tag "vegetable"). Read catalog_overview in the result and answer from it.
- Never invent a product id. `item_id` must be copied exactly from a
  search_catalog result in this conversation — never a line_id, never a number,
  never a guess. If you do not have one to hand, call search_catalog again.
- Respect any price ceiling the shopper states, as a per-unit ceiling in paise.
- Quantities are whole numbers. "2 kg" of a 1 kg pack means qty=2.
- Before adding anything, read the quantity and the variant out of what the
  shopper actually said:
    quantity is STATED by any number ("2", "three", "2 kg") or by "a", "an",
      "one", "a couple", "a few";
    variant is STATED when they name a size or pack, or when the product has
      no variants at all.
  Read them from the WHOLE conversation, not just the latest message — what
  they said earlier and what you yourself proposed both count.
  If both are settled, ACT — add it straight away and do not ask anything.
  Only when one is genuinely missing: search first, then reply with a single
  short question about just the missing piece, and do NOT call add_to_cart on
  that turn.
- A question you asked is answered by their next reply, INCLUDING a bare
  agreement: "yes", "yeah", "ok", "okay", "okayy", "sure", "go ahead", "do it",
  "add it", "please", "that one". When you proposed a specific product, pack or
  size and they agree, add exactly what you proposed, quantity 1 unless they
  said otherwise. Never re-ask a question they have already answered, and never
  call the quantity missing just because their agreement had no number in it.
- A QUESTION IS NOT AN INSTRUCTION. "did you add it", "is it in my cart",
  "what is in my basket", "how much is it" ask you to report, not to change
  anything. Call view_cart and answer from what it returns. Never add, update
  or remove anything on a turn where the shopper only asked a question.
- Add each thing the shopper asked for ONCE. If it is already in the cart and
  they want more, call update_qty with the new total — do not add it again.
    "add 2 kg lemons under Rs 100" -> quantity 2, no variants -> add it
    "add a medium tee"             -> quantity 1, size M      -> add it
    "add three hoodies in L"       -> quantity 3, size L      -> add it
    "add lemons"                   -> quantity missing        -> ask how many
    "add a kurti"                  -> size missing            -> ask which size
- Pass `variant` only when search_catalog showed that product has a "variants"
  list, and then only a label from that list. If it showed a plain "stock"
  number instead, omit `variant` entirely — a weight or pack size in the
  shopper's words ("2 kg") is a quantity, not a variant.
- If stock is short, add what is available and say so plainly.
- You cannot apply coupons, reserve stock, reorder past baskets or take payment
  yet. If asked, say that plainly. Do not pretend to have done it.
- When you are done, reply in one or two short sentences, quoting any amount
  from a *_display string exactly as given. No emoji, no exclamation marks,
  no salesy language.
"""


class LLMUnavailable(Exception):
    """The model could not be reached. Callers fall back deterministically."""


def _client() -> openai.OpenAI:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise LLMUnavailable("OPENAI_API_KEY is not set")
    return openai.OpenAI(api_key=key, timeout=30.0, max_retries=1)


def plan(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One model turn. Returns {content, tool_calls:[{id,name,args}]}.

    Raises LLMUnavailable on any API failure so the caller can degrade.
    """
    try:
        resp = _client().chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto", temperature=0.2
        )
    except Exception as exc:  # network, auth, rate limit, bad gateway
        raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

    msg = resp.choices[0].message
    calls: list[dict[str, Any]] = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except ValueError:
            args = {}
        calls.append({"id": tc.id, "name": tc.function.name, "args": args})
    return {"content": msg.content or "", "tool_calls": calls, "model": resp.model}


# -- deterministic fallback (spec 3) ---------------------------------------
# Only reached when the API is unreachable. Everything it returns is flagged
# degraded, so the UI never passes it off as the agent reasoning.

_QTY = re.compile(r"\b(\d+)\s*(?:kg|kgs|kilo|kilos|x|units?|pieces?|packs?)?\b", re.I)
_PRICE = re.compile(r"(?:under|below|less than|upto|up to|max)\s*(?:rs\.?|inr|₹)?\s*(\d+)", re.I)
_STOP = {
    "add", "to", "my", "cart", "basket", "under", "below", "please", "buy", "get", "want",
    "kg", "kgs", "kilo", "kilos", "the", "a", "an", "some", "of", "for", "rs", "inr", "and",
    "less", "than", "upto", "up", "max", "me", "i", "would", "like", "need",
}


def deterministic_fallback(user_text: str) -> dict[str, Any]:
    """A plain search-and-add plan so a dead API still demos. Not the agent."""
    text = user_text.strip()
    price = _PRICE.search(text)
    max_paise = int(price.group(1)) * 100 if price else None

    # strip any price clause before reading a quantity out of the sentence
    without_price = _PRICE.sub(" ", text)
    qty_match = _QTY.search(without_price)
    qty = max(1, int(qty_match.group(1))) if qty_match else 1

    words = [w for w in re.findall(r"[a-zA-Z]+", without_price) if w.lower() not in _STOP]
    query = " ".join(words[:3])

    if not query:
        return {
            "content": "The assistant is offline right now, so I can only handle simple "
                       "requests like 'add 2 lemons'. Tell me an item and a quantity.",
            "tool_calls": [],
            "degraded": True,
        }
    args: dict[str, Any] = {
        "query": query,
        "reason": "offline fallback: searching for what the sentence named",
    }
    if max_paise is not None:
        args["max_price_paise"] = max_paise
    return {
        "content": "",
        "tool_calls": [{"id": "fallback_search", "name": "search_catalog", "args": args}],
        "degraded": True,
        "fallback_qty": qty,
    }
