"""Tests for batch_hash / reduce_hash / options_hash."""

from __future__ import annotations

from analyzetg.analyzer.hasher import batch_hash, options_hash, reduce_hash


def test_batch_hash_stable_ordering() -> None:
    a = batch_hash("summary", "v1", "gpt-4o", [3, 1, 2], {"x": 1})
    b = batch_hash("summary", "v1", "gpt-4o", [1, 2, 3], {"x": 1})
    assert a == b


def test_batch_hash_differs_on_model() -> None:
    a = batch_hash("summary", "v1", "gpt-4o", [1, 2], None)
    b = batch_hash("summary", "v1", "gpt-4o-mini", [1, 2], None)
    assert a != b


def test_batch_hash_differs_on_version() -> None:
    a = batch_hash("summary", "v1", "gpt-4o", [1, 2], None)
    b = batch_hash("summary", "v2", "gpt-4o", [1, 2], None)
    assert a != b


def test_batch_hash_differs_on_options() -> None:
    a = batch_hash("summary", "v1", "gpt-4o", [1, 2], {"min_msg_chars": 3})
    b = batch_hash("summary", "v1", "gpt-4o", [1, 2], {"min_msg_chars": 5})
    assert a != b


def test_options_hash_order_independent() -> None:
    assert options_hash({"a": 1, "b": 2}) == options_hash({"b": 2, "a": 1})


def test_reduce_hash_stable_for_permuted_map_hashes() -> None:
    a = reduce_hash("summary", "v1", "gpt-4o", ["h1", "h2", "h3"])
    b = reduce_hash("summary", "v1", "gpt-4o", ["h3", "h1", "h2"])
    assert a == b


def test_reduce_hash_changes_when_set_changes() -> None:
    a = reduce_hash("summary", "v1", "gpt-4o", ["h1", "h2"])
    b = reduce_hash("summary", "v1", "gpt-4o", ["h1", "h2", "h3"])
    assert a != b
