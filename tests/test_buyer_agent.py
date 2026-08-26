"""The consumer buyer agent (spec 8, 4.4, 4.5).

This agent works for the shopper, not for either shop, so the tests are mostly
about restraint: it must not hide an option it dislikes, must not turn a
missing budget into a refusal, must not let a rule-breaking option be bought,
and must not lose the thread when the page is refreshed.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import buyer
from agent.app import create_app as create_agent_app
from common import chainlog, mandate, trust


@pytest.fixture
def agent_client(env) -> TestClient:
    return TestClient(create_agent_app(), raise_server_exceptions=True)


@pytest.fixture
def both_shops(freshkart, loomcraft, monkeypatch):
    """Point every outbound shop call at the two in-process shops.

    Two places reach out: agent/buyer.py uses an httpx.Client for discovery,
    and agent/app.py uses module-level httpx calls to build the basket and
    place the order. Both are routed here, or the test quietly talks to
    whatever happens to be listening on the real ports.
    """
    import agent.app as app_mod
    import agent.buyer as buyer_mod

    routes = {"http://127.0.0.1:8001": freshkart, "http://127.0.0.1:8002": loomcraft}

    def split(url: str):
        for base, client in routes.items():
            if url.startswith(base):
                return client, url[len(base):] or "/"
        raise AssertionError(f"test tried to reach {url}, which is not an in-process shop")

    class Client:
        def __init__(self, base_url: str, timeout: int = 0) -> None:
            self.inner = routes[base_url]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, path: str, **kw):
            return self.inner.get(path, **kw)

        def post(self, path: str, json=None, **kw):
            return self.inner.post(path, json=json, **kw)

    class Router:
        @staticmethod
        def get(url: str, **kw):
            client, path = split(url)
            kw.pop("timeout", None)
            return client.get(path, **kw)

        @staticmethod
        def post(url: str, json=None, **kw):
            client, path = split(url)
            kw.pop("timeout", None)
            return client.post(path, json=json, **kw)

        @staticmethod
        def patch(url: str, json=None, **kw):
            client, path = split(url)
            kw.pop("timeout", None)
            return client.patch(path, json=json, **kw)

    Router.Client = Client
    monkeypatch.setattr(buyer_mod.httpx, "Client", Client)
    monkeypatch.setattr(app_mod, "httpx", Router)
    return routes


# -- reading the goal -------------------------------------------------------

def test_a_budget_is_read_however_it_is_phrased():
    for text in ("cotton kurti under 1500", "cotton kurti under Rs 1,500",
                 "cotton kurti below ₹1500", "cotton kurti, budget 1500"):
        assert buyer.parse_goal(text)["budget_paise"] == 150000, text


def test_a_size_and_an_exactness_rule_are_read():
    rules = buyer.parse_goal("cotton kurti size M under 1500, exact brand only")
    assert rules["size"] == "M"
    assert rules["exact_only"] is True
    assert "kurti" in rules["query"]


def test_the_budget_words_do_not_leak_into_the_search():
    rules = buyer.parse_goal("lemons under Rs 100")
    assert "under" not in rules["query"] and "100" not in rules["query"]
    assert rules["query"] == "lemons"


def test_a_missing_budget_is_a_clarification_not_a_refusal(agent_client, both_shops):
    """Spec 8: red Blocked cards are for over-cap, invalid mandate, price
    mismatch and injection. Not knowing what someone wants to spend is none
    of those."""
    run = agent_client.post("/buyer/run", json={"goal": "a cotton kurti"}).json()
    assert run["status"] == "needs_clarification"
    kinds = [m["kind"] for m in run["messages"]]
    assert "ask" in kinds
    assert "blocked" not in kinds


# -- ranking, and refusing to hide things -----------------------------------

def test_options_come_from_both_shops(agent_client, both_shops):
    run = agent_client.post("/buyer/run", json={"goal": "cotton under 2000"}).json()
    shops = {o["shop_id"] for o in run["options"]}
    assert len(shops) >= 1
    assert run["status"] in ("options", "all_rule_breaking")


def test_the_mandate_is_issued_from_the_stated_budget(agent_client, both_shops):
    run = agent_client.post("/buyer/run", json={"goal": "kurti under 1500"}).json()
    claims = mandate.verify(run["mandate_token"])
    assert claims["max_per_txn"] == 150000
    assert claims["max_total"] == 150000


def test_an_over_budget_option_is_shown_greyed_with_the_reason(agent_client, both_shops):
    """It is the shopper's call whether to break their own rule, but they can
    only make it if they are told the option exists (spec 8)."""
    run = agent_client.post("/buyer/run", json={"goal": "kurti under 100"}).json()
    breakers = [o for o in run["options"] if not o["selectable"]]
    assert breakers, "an over-budget match should still be listed"
    assert any("over your" in r for o in breakers for r in o["breaks_rules"])


def test_a_greyed_option_is_refused_by_the_server_not_just_the_styling(agent_client, both_shops):
    run = agent_client.post("/buyer/run", json={"goal": "kurti under 100"}).json()
    greyed = next(o for o in run["options"] if not o["selectable"])
    resp = agent_client.post(f"/buyer/run/{run['run_id']}/choose",
                             json={"option_id": greyed["option_id"]})
    assert resp.status_code >= 400
    assert "breaks a rule you set" in resp.json()["why"]


def test_the_ranking_weights_are_the_ones_the_spec_states():
    assert (buyer.W_PRICE, buyer.W_RULE_FIT, buyer.W_TRUST, buyer.W_AVAILABILITY) == (
        0.4, 0.3, 0.2, 0.1)
    assert sum([buyer.W_PRICE, buyer.W_RULE_FIT, buyer.W_TRUST, buyer.W_AVAILABILITY]) == 1.0


def test_a_cheaper_option_outranks_a_dearer_one_all_else_equal():
    rules = buyer.parse_goal("tee under 2000")
    options = [
        {"option_id": "a", "shop_id": "s1", "shop_name": "S1", "shop_url": "u", "item_id": "i1",
         "name": "Dear Tee", "category": "c", "variant": "", "price_paise": 190000,
         "stock": 5, "restock_date": None, "in_stock": True, "can_reserve": False,
         "match_strength": 1, "breaks_rules": [], "selectable": True, "trust": 0.7},
        {"option_id": "b", "shop_id": "s2", "shop_name": "S2", "shop_url": "u", "item_id": "i2",
         "name": "Cheap Tee", "category": "c", "variant": "", "price_paise": 50000,
         "stock": 5, "restock_date": None, "in_stock": True, "can_reserve": False,
         "match_strength": 1, "breaks_rules": [], "selectable": True, "trust": 0.7},
    ]
    ranked = buyer.rank(options, rules)
    assert ranked[0]["name"] == "Cheap Tee"


def test_rule_breakers_always_sort_below_anything_selectable():
    rules = buyer.parse_goal("tee under 1000")
    options = [
        {"option_id": "a", "shop_id": "s1", "shop_name": "S1", "shop_url": "u", "item_id": "i1",
         "name": "Cheap But Wrong", "category": "c", "variant": "", "price_paise": 10000,
         "stock": 5, "restock_date": None, "in_stock": True, "can_reserve": False,
         "match_strength": 2, "breaks_rules": ["wrong size"], "selectable": False, "trust": 1.0},
        {"option_id": "b", "shop_id": "s2", "shop_name": "S2", "shop_url": "u", "item_id": "i2",
         "name": "Fine", "category": "c", "variant": "", "price_paise": 90000,
         "stock": 1, "restock_date": None, "in_stock": True, "can_reserve": False,
         "match_strength": 1, "breaks_rules": [], "selectable": True, "trust": 0.2},
    ]
    ranked = buyer.rank(options, rules)
    assert ranked[0]["name"] == "Fine"      # despite being dearer and less trusted


def test_every_option_explains_itself(agent_client, both_shops):
    run = agent_client.post("/buyer/run", json={"goal": "cotton under 2000"}).json()
    for o in run["options"]:
        assert o["why"].strip()
        assert set(o["score_parts"]) == {"price", "rule_fit", "trust", "availability"}


# -- the thread survives a refresh (spec 8) ---------------------------------

def test_a_run_is_restored_by_its_id(agent_client, both_shops):
    run = agent_client.post("/buyer/run", json={"goal": "kurti under 2000"}).json()
    again = agent_client.get(f"/buyer/run/{run['run_id']}").json()
    assert again["run_id"] == run["run_id"]
    assert again["goal"] == run["goal"]
    assert [o["option_id"] for o in again["options"]] == [o["option_id"] for o in run["options"]]


def test_a_run_outlives_the_process_that_created_it(agent_client, both_shops, env):
    """Run state is on disk, so a refresh after a restart still restores it -
    the same lesson the order history had to learn."""
    run = agent_client.post("/buyer/run", json={"goal": "kurti under 2000"}).json()
    fresh = TestClient(create_agent_app(), raise_server_exceptions=True)
    restored = fresh.get(f"/buyer/run/{run['run_id']}").json()
    assert restored["goal"] == run["goal"]
    assert restored["options"]


def test_an_unknown_run_is_a_clean_404(agent_client):
    assert agent_client.get("/buyer/run/buy_nope").json()["code"] == "NOT_FOUND"


# -- trust moves, and is felt (spec 4.5) ------------------------------------

def test_trust_starts_neutral_and_rises_slowly():
    assert trust.score("nobody") == 0.7
    trust.record("shopx", "clean", "paid as quoted")
    assert trust.score("shopx") == pytest.approx(0.75)


def test_a_violation_halves_trust_immediately():
    trust.record("shopy", "clean", "fine")          # 0.75
    moved = trust.record("shopy", "price_mismatch", "charged more than approved")
    assert moved["score_after"] == pytest.approx(0.375)
    assert trust.score("shopy") == pytest.approx(0.375)


def test_trust_is_remembered_across_processes():
    trust.record("shopz", "clean", "paid as quoted")
    before = trust.score("shopz")
    import importlib

    import common.trust as t
    importlib.reload(t)
    assert t.score("shopz") == before


def test_a_less_trusted_shop_is_outranked_when_prices_match():
    rules = buyer.parse_goal("tee under 2000")
    base = {"shop_url": "u", "category": "c", "variant": "", "price_paise": 100000,
            "stock": 5, "restock_date": None, "in_stock": True, "can_reserve": False,
            "match_strength": 1, "breaks_rules": [], "selectable": True}
    ranked = buyer.rank([
        {**base, "option_id": "a", "shop_id": "shady", "shop_name": "Shady",
         "item_id": "i1", "name": "Tee A", "trust": 0.35},
        {**base, "option_id": "b", "shop_id": "solid", "shop_name": "Solid",
         "item_id": "i2", "name": "Tee B", "trust": 1.0},
    ], rules)
    assert ranked[0]["shop_name"] == "Solid"


# -- history is answered, not refused (spec 8) ------------------------------

def test_history_is_answered_from_the_chain_not_a_red_card(agent_client):
    chainlog.append("buyer", "payment_created", "paid 12300 paise at someshop",
                    {"shop_id": "someshop", "amount_paise": 12300})
    body = agent_client.get("/buyer/history").json()
    assert any(e["event"] == "payment_created" for e in body["entries"])
    assert "trust" in body


def test_a_partial_word_match_does_not_outrank_the_thing_asked_for():
    """"cotton kurti" pulled in cotton socks, and because socks are cheap and
    price is the heaviest weight, the socks ranked first. Arithmetically
    correct, obviously useless."""
    catalog = [
        {"id": "socks", "name": "Crew Socks (3-pack)", "description": "combed cotton",
         "category": "accessories", "tags": [], "price_paise": 29900},
        {"id": "kurti", "name": "Indigo Cotton Kurti", "description": "mulmul cotton kurti",
         "category": "women", "tags": ["kurti"], "price_paise": 149900},
    ]
    picked = buyer._candidates(catalog, "cotton kurti")
    assert [p["id"] for p, _ in picked] == ["kurti"]


def test_a_partial_match_is_still_offered_when_nothing_matches_fully():
    """A near miss beats an empty page."""
    catalog = [
        {"id": "socks", "name": "Crew Socks", "description": "combed cotton",
         "category": "accessories", "tags": [], "price_paise": 29900},
    ]
    picked = buyer._candidates(catalog, "cotton kurti")
    assert [p["id"] for p, _ in picked] == ["socks"]


def test_an_empty_query_matches_nothing_rather_than_everything():
    catalog = [{"id": "x", "name": "Thing", "description": "", "category": "",
                "tags": [], "price_paise": 100}]
    assert buyer._candidates(catalog, "") == []


# -- a merchant that cheats is caught, refused, and demoted -----------------

def test_a_shop_that_charges_more_than_approved_is_blocked_and_loses_trust(
        agent_client, both_shops, loomcraft, monkeypatch):
    """The whole loop: the shop quotes honestly, the human approves that, the
    shop then asks for more, the wallet refuses, the shopper sees a red card,
    and the shop's ranking weight drops for next time (spec 4.5, 8)."""
    from common import wallet

    run = agent_client.post("/buyer/run", json={"goal": "graphic tee size M under 1500"}).json()
    option = next(o for o in run["options"] if o["selectable"])
    run = agent_client.post(f"/buyer/run/{run['run_id']}/choose",
                            json={"option_id": option["option_id"]}).json()
    assert run["status"] == "awaiting_approval"
    honest = run["quote"]["charge_amount"]

    # only now does the merchant get greedy
    loomcraft.post("/admin/cheat-mode", json={"on": True})
    monkeypatch.setattr(wallet, "_fetch_charge",
                        lambda url, txn: loomcraft.get(f"/order/{txn}").json())

    before = trust.score("loomcraft")
    run = agent_client.post(f"/buyer/run/{run['run_id']}/approve", json={}).json()

    assert run["status"] == "blocked"
    card = run["messages"][-1]
    assert card["kind"] == "blocked"
    assert card["code"] == "PRICE_CHANGED"
    assert trust.score("loomcraft") == pytest.approx(before * 0.5)
    assert run.get("receipt") is None

    # and the order is still unpaid: nothing moved
    assert loomcraft.get(f"/order/{run['quote']['txn_ref']}").json()["status"] == "pending"
    assert honest < loomcraft.get(f"/order/{run['quote']['txn_ref']}").json()["charge_amount"]


def test_a_demoted_shop_ranks_worse_afterwards():
    """Trust is 20% of the score, so being caught is felt in the next search
    rather than only recorded in a log nobody opens."""
    rules = buyer.parse_goal("tee under 2000")
    base = {"shop_url": "u", "category": "c", "variant": "", "price_paise": 100000,
            "stock": 5, "restock_date": None, "in_stock": True, "can_reserve": False,
            "match_strength": 1, "breaks_rules": [], "selectable": True}
    honest = {**base, "option_id": "a", "shop_id": "honest", "shop_name": "Honest",
              "item_id": "i1", "name": "Tee", "trust": 0.75}
    caught = {**base, "option_id": "b", "shop_id": "caught", "shop_name": "Caught",
              "item_id": "i2", "name": "Tee", "trust": 0.375}
    ranked = buyer.rank([caught, honest], rules)
    assert ranked[0]["shop_name"] == "Honest"
    assert ranked[0]["score"] > ranked[1]["score"]
