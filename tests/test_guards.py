"""Structural guards from spec 0.6 and 3: common/wallet.py stays under 80
lines, fully typed, with zero LLM imports, and it is the only module in the
codebase permitted to import the Razorpay SDK."""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WALLET = REPO / "common" / "wallet.py"
LLM_MODULES = {"openai", "anthropic", "langchain", "litellm", "llm"}
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".git", "data"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def _all_py_files() -> list[Path]:
    return [p for p in REPO.rglob("*.py") if not (set(p.relative_to(REPO).parts) & SKIP_DIRS)]


def test_wallet_under_80_lines():
    n = len(WALLET.read_text(encoding="utf-8").splitlines())
    assert n < 80, f"common/wallet.py is {n} lines; spec caps it below 80"


def test_wallet_has_zero_llm_imports():
    assert not (_imports(WALLET) & LLM_MODULES)


def test_only_wallet_imports_razorpay_sdk():
    offenders = [str(p.relative_to(REPO)) for p in _all_py_files()
                 if p != WALLET and "razorpay" in _imports(p)]
    assert offenders == [], f"razorpay SDK imported outside common/wallet.py: {offenders}"
    assert "razorpay" in _imports(WALLET)  # and the wallet actually uses it


def test_wallet_is_fully_typed():
    tree = ast.parse(WALLET.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.returns is not None, f"{node.name} lacks a return annotation"
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for a in args:
                assert a.annotation is not None, f"{node.name} arg '{a.arg}' lacks an annotation"


LLM_MODULE = REPO / "agent" / "llm.py"


def test_only_agent_llm_imports_the_llm_sdk():
    """Spec 3: every LLM call is isolated in agent/llm.py, so a dead API can
    be degraded in exactly one place."""
    offenders = [str(p.relative_to(REPO)) for p in _all_py_files()
                 if p != LLM_MODULE and _imports(p) & LLM_MODULES]
    assert offenders == [], f"LLM SDK imported outside agent/llm.py: {offenders}"
    assert _imports(LLM_MODULE) & LLM_MODULES  # and it really is the one that does


def test_trust_core_has_no_llm_imports():
    """Nothing on the money path may import an LLM (spec 0.6)."""
    for rel in ("common/wallet.py", "common/mandate.py", "common/approval.py",
                "common/chainlog.py", "shop/app.py", "shop/coupons.py", "agent/tools.py"):
        assert not (_imports(REPO / rel) & LLM_MODULES), f"{rel} must not import an LLM"
