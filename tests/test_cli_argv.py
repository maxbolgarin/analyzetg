"""Tests for the CLI argv preprocessor that escapes negative-numeric ids."""

from __future__ import annotations

import sys

from analyzetg.cli import _preprocess_argv


def _run_with(argv: list[str]) -> list[str]:
    saved = sys.argv
    try:
        sys.argv = argv
        _preprocess_argv()
        return sys.argv
    finally:
        sys.argv = saved


def test_no_negative_number_unchanged() -> None:
    assert _run_with(["analyzetg", "analyze", "@foo"]) == [
        "analyzetg",
        "analyze",
        "@foo",
    ]


def test_negative_number_gets_double_dash_injected() -> None:
    # Before: ['analyzetg', 'analyze', '-1003865481227']
    # After:  ['analyzetg', 'analyze', '--', '-1003865481227']
    assert _run_with(["analyzetg", "analyze", "-1003865481227"]) == [
        "analyzetg",
        "analyze",
        "--",
        "-1003865481227",
    ]


def test_existing_double_dash_not_duplicated() -> None:
    assert _run_with(["analyzetg", "analyze", "--", "-1003865481227"]) == [
        "analyzetg",
        "analyze",
        "--",
        "-1003865481227",
    ]


def test_flags_after_ref_preserved() -> None:
    assert _run_with(["analyzetg", "analyze", "-1003865481227", "--preset", "digest"]) == [
        "analyzetg",
        "analyze",
        "--",
        "-1003865481227",
        "--preset",
        "digest",
    ]


def test_short_flags_not_touched() -> None:
    # `-c` is a real short flag (console); don't confuse it with a number.
    assert _run_with(["analyzetg", "analyze", "@foo", "-c"]) == [
        "analyzetg",
        "analyze",
        "@foo",
        "-c",
    ]
