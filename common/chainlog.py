"""SHA-256 hash chain, one append-only JSONL file per actor (spec 4.2).

hash = sha256(prev_hash + canonical_json(entry_without_hash)); genesis prev
is 64 zeros. verify_chain() reports the first bad index. A lock file with a
30-second stale-break serialises writers (Windows-safe, no fcntl).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS: str = "0" * 64


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chains_dir() -> Path:
    d = Path(os.environ.get("VELCROW_DATA_DIR", "data")) / "chains"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chain_path(actor: str) -> Path:
    return _chains_dir() / f"{actor}.jsonl"


class _Lock:
    def __init__(self, actor: str, stale_after: float = 30.0) -> None:
        self.path = _chains_dir() / f"{actor}.lock"
        self.stale_after = stale_after

    def __enter__(self) -> "_Lock":
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_after:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"chain lock stuck: {self.path}")
                time.sleep(0.02)

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


def _read_entries(actor: str) -> list[dict[str, Any]]:
    path = chain_path(actor)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def append(actor: str, event: str, why: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append one entry. `why` is the human-readable reason — always required."""
    if not why:
        raise ValueError("chain-log entries must carry a human-readable why")
    with _Lock(actor):
        entries = _read_entries(actor)
        prev_hash = entries[-1]["hash"] if entries else GENESIS
        entry: dict[str, Any] = {
            "i": len(entries),
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "event": event,
            "why": why,
            "data": data,
            "prev_hash": prev_hash,
        }
        entry["hash"] = hashlib.sha256((prev_hash + canonical_json(entry)).encode("utf-8")).hexdigest()
        with chain_path(actor).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def verify_chain(actor: str) -> tuple[bool, int | None]:
    """Recompute every hash and link. Returns (ok, first_bad_index)."""
    prev_hash = GENESIS
    for i, entry in enumerate(_read_entries(actor)):
        claimed = entry.get("hash")
        body = {k: v for k, v in entry.items() if k != "hash"}
        if body.get("prev_hash") != prev_hash:
            return False, i
        expected = hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()
        if claimed != expected:
            return False, i
        prev_hash = claimed
    return True, None


def tail(actor: str, n: int = 5) -> list[dict[str, Any]]:
    return _read_entries(actor)[-n:]
