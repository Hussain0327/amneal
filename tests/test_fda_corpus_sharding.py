from __future__ import annotations

import pytest

from regwatch.corpus.sharding import (
    FDA_CORPUS_SHARD_COUNT,
    corpus_shard_id,
    parse_shard_partition_key,
    shard_partition_key,
)


def test_corpus_shard_is_stable_and_bounded() -> None:
    canonical_id = "drugs-at-fda:application-doc:20685"
    assert corpus_shard_id(canonical_id) == corpus_shard_id(canonical_id)
    assert 0 <= corpus_shard_id(canonical_id) < FDA_CORPUS_SHARD_COUNT


def test_partition_keys_round_trip_all_shards() -> None:
    assert [parse_shard_partition_key(shard_partition_key(i)) for i in range(512)] == list(
        range(512)
    )


@pytest.mark.parametrize("value", ["", "1", "0512", "512", "-01", "abc"])
def test_invalid_partition_key_fails_closed(value: str) -> None:
    with pytest.raises(ValueError):
        parse_shard_partition_key(value)
