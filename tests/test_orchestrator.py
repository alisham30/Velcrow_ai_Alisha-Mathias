"""The orchestrator: routing, scheduling, and handoffs in one readable place.

Not a framework - the point. Each test pins an edge of the coordination
graph: routing falls back deterministically, handoffs land on the chain,
and the unattended agents are registered from exactly one function.
"""
from __future__ import annotations

from typing import Any

from agent import orchestrator
from common import chainlog


def test_the_fleet_is_declared_with_powers_and_prohibitions(env):
    fleet = orchestrator.status()["agents"]
    assert {"shopping-assistant", "buyer-agent", "growth-agent",
            "message-router", "outreach"} <= set(fleet)
    for agent in fleet.values():      # every agent names what it CANNOT do
        assert agent["cannot"]


def test_route_falls_back_when_the_model_is_down(env):
    # no OPENAI key in tests -> llm raises -> the deterministic fallback answers
    got = orchestrator.route("1 kg lemons", "grocery",
                             {"grocery": "FreshKart", "apparel": "Loomcraft"},
                             lambda text, cur: "grocery")
    assert got == {"mode": "shop", "shop": "grocery"}


def test_handoffs_land_on_the_chain(env):
    orchestrator.handoff("whatsapp", "buyer-agent", "routed 'kurti under 1500' as a goal")
    events = [e for e in chainlog.tail("buyer", 10)
              if e["event"] == "orchestrator_handoff"]
    assert events and events[-1]["data"]["to_agent"] == "buyer-agent"
    assert orchestrator.status()["recent_handoffs"]


def test_the_unattended_agents_are_registered_in_one_place(env):
    class FakeSched:
        def __init__(self) -> None:
            self.jobs: list[dict[str, Any]] = []

        def add_job(self, fn, kind, **kw) -> None:
            self.jobs.append({"fn": fn, "kind": kind, **kw})

    sched = FakeSched()
    shops = {"grocery": {"shop_id": "freshkart"}, "apparel": {"shop_id": "loomcraft"}}
    orchestrator.register_schedules(sched, shops, lambda *a: None, lambda: None)
    ids = {j["id"] for j in sched.jobs}
    assert ids == {"growth-freshkart", "growth-loomcraft", "whatsapp-abandoned-sweep"}
    assert all(j["max_instances"] == 1 for j in sched.jobs)   # never self-overlapping
