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


RUPEE = "₹"


def test_rupee_sign_round_trips_through_the_chain():
    """The chain is evidence, so a money figure must come back out of it as
    the exact character that went in - on any platform, read by any tool."""
    why = f"best Indigo Cotton Kurti at {RUPEE}1,499.00"
    written = chainlog.append("buyer", "agent_tool_call", why, {"subtotal": f"{RUPEE}88.00"})
    assert written["why"] == why

    (read_back,) = chainlog.tail("buyer", 1)
    assert read_back["why"] == why
    assert read_back["why"].count(RUPEE) == 1
    assert read_back["data"]["subtotal"] == f"{RUPEE}88.00"

    # The bytes on disk carry no raw UTF-8, so no cp1252-defaulting reader can
    # turn the rupee sign into mojibake. That was the actual regression: a
    # BOM-less UTF-8 file is read as cp1252 by the default Windows toolchain,
    # and one character becomes three.
    raw = chainlog.chain_path("buyer").read_bytes()
    assert raw.isascii(), "chain log must be written as pure ASCII escapes"
    assert RUPEE.encode("utf-8") not in raw
    assert b"\\u20b9" in raw

    ok, bad = chainlog.verify_chain("buyer")
    assert ok is True and bad is None


def test_escaped_write_does_not_change_hashes():
    """Escaping is a serialisation choice only: the hash is taken over the
    canonical form, so chains written before the change still verify."""
    a = chainlog.append("buyer", "e", f"plain ascii", {})
    b = chainlog.append("buyer", "e", f"{RUPEE}1,000.00", {})
    assert b["prev_hash"] == a["hash"]
    assert chainlog.verify_chain("buyer") == (True, None)


def test_no_source_file_contains_mojibake():
    """A UTF-8 string round-tripped through cp1252 leaves a recognisable
    signature in the source. One had shipped in a user-visible error string
    in shop/app.py, so it reached the shopper and the chain log.

    The signatures are computed rather than written out, so this file does
    not trip its own scan.
    """
    import pathlib

    # em dash, rupee sign, right single quote -- put through the exact trip
    # that produces the corruption: UTF-8 bytes decoded as cp1252.
    originals = [chr(0x2014), chr(0x20B9), chr(0x2019)]
    signatures = [c.encode("utf-8").decode("cp1252", errors="replace") for c in originals]
    skip_parts = {"node_modules", ".venv", "dist", ".git"}
    root = pathlib.Path(__file__).resolve().parent.parent

    offenders = []
    for pattern in ("*.py", "*.js", "*.jsx"):
        for path in root.rglob(pattern):
            if skip_parts & set(path.parts):
                continue
            text = path.read_text(encoding="utf-8-sig")
            for sig in signatures:
                if sig in text:
                    offenders.append(f"{path.relative_to(root)}: {sig!r}")
    assert not offenders, f"mojibake in source: {offenders}"


def test_chains_are_per_actor():
    chainlog.append("buyer", "e", "buyer entry", {})
    chainlog.append("freshkart", "e", "shop entry", {})
    assert len(chainlog.tail("buyer", 10)) == 1
    assert len(chainlog.tail("freshkart", 10)) == 1
