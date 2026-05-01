"""Cache-key hashing for analysis (spec §9.2)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def options_hash(options: dict[str, Any] | None) -> str:
    if not options:
        return ""
    # Sorted JSON so equivalent dicts hash identically
    canonical = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # 16 hex chars = 64 bits. Birthday-bound 50% collision at ~2^32 ≈ 4B
    # distinct option dicts — far above any single user's lifetime cache,
    # so safe. Don't truncate further; collisions here cause silent stale
    # cache hits in `analysis_cache`.
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def text_hash(*parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    # Same 64-bit budget as options_hash; see note there.
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def batch_hash(
    preset: str,
    prompt_version: str,
    model: str,
    msg_ids: Iterable[int],
    options: dict[str, Any] | None = None,
) -> str:
    ids_sorted = ",".join(str(i) for i in sorted({int(i) for i in msg_ids}))
    payload = f"{preset}|{prompt_version}|{model}|{ids_sorted}|{options_hash(options)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reduce_hash(
    preset: str,
    prompt_version: str,
    model: str,
    map_hashes: Iterable[str],
    options: dict[str, Any] | None = None,
) -> str:
    joined = ",".join(sorted(map_hashes))
    payload = f"reduce|{preset}|{prompt_version}|{model}|{joined}|{options_hash(options)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
