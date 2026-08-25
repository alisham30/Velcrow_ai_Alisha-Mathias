"""Loads a shop config (grocery/apparel) and its catalog file. One codebase,
two shops, differentiated purely by this config (spec 2, 6.1)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

SHOP_DIR = Path(__file__).parent


def load_config(shop_kind: str) -> dict[str, Any]:
    path = SHOP_DIR / "configs" / f"{shop_kind}.yaml"
    if not path.exists():
        raise RuntimeError(f"unknown shop config '{shop_kind}' (expected {path})")
    with path.open("r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f)
    cfg["kind"] = shop_kind
    return cfg


def load_catalog(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    path = SHOP_DIR / cfg["catalog_file"]
    with path.open("r", encoding="utf-8") as f:
        products: list[dict[str, Any]] = json.load(f)
    for p in products:
        if not isinstance(p["price_paise"], int) or not isinstance(p["cost_price_paise"], int):
            raise ValueError(f"catalog prices must be integer paise: {p['id']}")
    return products
