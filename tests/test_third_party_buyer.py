"""The interoperability proof (spec 6.6).

`lab/third_party_buyer.py` is the acceptance artifact for the claim that these
merchants are sellable to ANY agent, not just to ours. That claim is only worth
making if the script genuinely has no private knowledge, so these tests check
the properties a sceptical judge would check: it imports nothing of ours, it
names neither shop, and every endpoint it calls is one the merchant's own
manifest declares.

The last one is the real test. A script that "only reads the manifest" while
calling an endpoint the manifest never mentions is relying on private
knowledge, and the demo line would be false.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "lab" / "third_party_buyer.py"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_it_names_neither_merchant():
    """Spec 6.6: 'It must not contain the word freshkart or loomcraft anywhere.'"""
    text = _source().lower()
    for name in ("freshkart", "loomcraft"):
        assert name not in text, f"the third-party buyer names {name!r}"


def test_it_imports_nothing_of_ours():
    """No agent/, no shop/, no common/ - it brings only stdlib and an HTTP client."""
    tree = ast.parse(_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"agent", "shop", "common", "lab"}), (
        f"the third-party buyer imports project code: {imported}")
    assert "httpx" in imported


def test_it_stays_small_enough_to_read_in_the_demo():
    """Spec 6.6 calls it a ~60-line client. The point is that a judge can read
    the whole thing on screen and satisfy themselves it hides nothing."""
    code = [ln for ln in _source().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert len(code) < 130, f"{len(code)} code lines is too long to read on camera"


def test_every_path_it_calls_is_declared_in_the_manifest(freshkart, loomcraft):
    """The load-bearing one. If the script calls something the manifest does
    not mention, then 'it reads only the manifest' is not true."""
    source = _source()
    # every f-string path the script builds against the shop
    called = set(re.findall(r'\{args\.shop_url\}(/[a-z0-9/_.\-]+)', source))
    # plus the ones it takes from the manifest itself rather than hard coding
    from_manifest = set(re.findall(r'\{m\[[\'"](\w+)[\'"]\]\}', source))
    assert "catalog" in from_manifest, "the catalog path must come from the manifest"

    for client in (freshkart, loomcraft):
        m = client.get("/.well-known/agent-commerce.json").json()
        declared = {m["catalog"], "/agent/capabilities", "/.well-known/agent-commerce.json"}
        for value in m["order"].values():
            if isinstance(value, str) and " " in value:
                declared.add(value.split(" ", 1)[1])      # "POST /order" -> "/order"

        for path in called:
            # template paths like /cart/{id} are declared as /cart/{cart_id}
            root = "/" + path.strip("/").split("/")[0]
            assert any(d == path or d.startswith(root) for d in declared), (
                f"{path} is called but not declared by {m['merchant']['id']}'s manifest")


def test_the_manifest_alone_describes_a_complete_purchase(freshkart):
    """A manifest that stops before the basket is documentation, not an
    interface - a stranger could discover the goods and still not buy them."""
    m = freshkart.get("/.well-known/agent-commerce.json").json()
    order = m["order"]
    for step in ("cart_create", "cart_update", "create", "read", "confirm"):
        assert order.get(step), f"the manifest does not say how to {step}"
    assert order["amounts"] == "integer paise"
    assert m["auth"]["presented_as"].startswith("Authorization: Mandate")
    assert "OUT_OF_STOCK" in m["errors"] and "PRICE_CHANGED" in m["errors"]


def test_the_two_manifests_differ_where_the_shops_differ(freshkart, loomcraft):
    """One script handles both because the manifests say different things -
    that difference is what it adapts to (spec 6.6)."""
    fresh = freshkart.get("/.well-known/agent-commerce.json").json()
    loom = loomcraft.get("/.well-known/agent-commerce.json").json()

    assert fresh["merchant"]["category"] != loom["merchant"]["category"]
    assert "reservations" in loom["capabilities"]
    assert "reservations" not in fresh["capabilities"]
    assert loom["order"]["reserve"] == "POST /reserve"
    assert fresh["order"]["reserve"] is None          # and it says so, rather than lying

    fresh_caps = freshkart.post("/agent/capabilities", json={"capabilities": {}}
                                ).json()["capabilities"]
    loom_caps = loomcraft.post("/agent/capabilities", json={"capabilities": {}}
                               ).json()["capabilities"]
    assert fresh_caps["variants"] == "pack"
    assert loom_caps["variants"] == "size"


def test_an_unsupported_capability_is_answered_not_ignored(freshkart):
    """A buyer that asks for reservations at a shop that has none gets a
    straight no, so it can adapt instead of discovering it at checkout."""
    answer = freshkart.post("/agent/capabilities",
                            json={"capabilities": {"reservations": True, "gift_wrap": True}}
                            ).json()["capabilities"]
    assert answer["reservations"] is False
    assert answer["gift_wrap"] is False
