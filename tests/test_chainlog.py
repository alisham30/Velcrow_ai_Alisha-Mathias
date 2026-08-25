from __future__ import annotations

import json

import pytest

from common import chainlog


def test_append_and_verify_green():
    for i in range(5):
        entry = chainlog.append("buyer", "test_event", f"reason {i}", {"n": i})
        assert entry["prev_hash"] == (chainlog.GENESIS if i == 0 else prev["hash"])
        prev = entry
    ok, bad = chainlog.verify_chain("buyer")
    assert ok is True and bad is None


def test_verify_empty_chain_is_green():
    assert chainlog.verify_chain("nobody") == (True, None)


def test_tampered_data_reports_first_bad_index():
    for i in range(4):
        chainlog.append("buyer", "e", f"r{i}", {"n": i})
    path = chainlog.chain_path("buyer")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[2])
    entry["data"]["n"] = 999  # tamper entry index 2
    lines[2] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, bad = chainlog.verify_chain("buyer")
    assert ok is False and bad == 2


def test_tampered_link_detected():
    for i in range(3):
        chainlog.append("shopx", "e", f"r{i}", {"n": i})
    path = chainlog.chain_path("shopx")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    del lines[1]  # remove a middle entry: link from entry 2 must break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, bad = chainlog.verify_chain("shopx")
    assert ok is False and bad == 1


def test_why_is_mandatory():
    with pytest.raises(ValueError):
        chainlog.append("buyer", "event", "", {})


def test_chains_are_per_actor():
    chainlog.append("buyer", "e", "buyer entry", {})
    chainlog.append("freshkart", "e", "shop entry", {})
    assert len(chainlog.tail("buyer", 10)) == 1
    assert len(chainlog.tail("freshkart", 10)) == 1
