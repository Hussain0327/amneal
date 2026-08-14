"""Stable partitioning for authoritative FDA corpus backfills."""

from __future__ import annotations

import hashlib

FDA_CORPUS_SHARD_COUNT = 512


def corpus_shard_id(canonical_id: str, *, shard_count: int = FDA_CORPUS_SHARD_COUNT) -> int:
    """Map one stable FDA identity to a reproducible, balanced shard."""

    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    normalized = canonical_id.strip()
    if not normalized:
        raise ValueError("canonical_id must not be blank")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False) % shard_count


def shard_partition_key(shard_id: int) -> str:
    if not 0 <= shard_id < FDA_CORPUS_SHARD_COUNT:
        raise ValueError(f"shard_id must be between 0 and {FDA_CORPUS_SHARD_COUNT - 1}")
    return f"{shard_id:03d}"


def parse_shard_partition_key(value: str) -> int:
    if len(value) != 3 or not value.isdigit():
        raise ValueError("FDA shard partition keys must contain exactly three digits")
    shard_id = int(value)
    if not 0 <= shard_id < FDA_CORPUS_SHARD_COUNT:
        raise ValueError(f"FDA shard partition key is outside 000-511: {value}")
    return shard_id
