"""Tests for the CLI argv preprocessor that escapes negative-numeric ids.

The preprocessor exists so users can type `atg analyze -1003865481227`
without Click mistaking the chat_id for a short-option bundle. Earlier
it injected `--` in-place, which broke
`atg analyze -1003865481227 --all-flat` (the `--` closed option parsing
and `--all-flat` became a stray second positional). Current design:
move negative-number **positionals** to the end of argv behind `--`,
leave flags in place.
"""

from __future__ import annotations

from analyzetg.cli import _preprocess_argv


def test_no_negative_number_unchanged() -> None:
    assert _preprocess_argv(["analyzetg", "analyze", "@foo"]) == [
        "analyzetg",
        "analyze",
        "@foo",
    ]


def test_bare_negative_number_moves_to_end() -> None:
    # Simplest case: user typed just the chat id, no other flags.
    assert _preprocess_argv(["analyzetg", "analyze", "-1003865481227"]) == [
        "analyzetg",
        "analyze",
        "--",
        "-1003865481227",
    ]


def test_negative_number_then_flag_regression() -> None:
    # The real-world regression: `atg analyze -1003865481227 --all-flat`.
    # Old behavior: `atg analyze -- -1003865481227 --all-flat` → Click
    # sees `--all-flat` after `--` as a second positional and errors
    # "unexpected extra argument (--all-flat)". New behavior keeps flags
    # in place and tucks the negative id behind a trailing `--`.
    assert _preprocess_argv(["analyzetg", "analyze", "-1003865481227", "--all-flat"]) == [
        "analyzetg",
        "analyze",
        "--all-flat",
        "--",
        "-1003865481227",
    ]


def test_flag_before_negative_number() -> None:
    # `atg analyze --all-flat -1003865481227` — negative id is still a
    # positional (prev is `--all-flat` which is a BOOLEAN flag, but our
    # heuristic treats any `-`-prefixed prev as a possible option value
    # and leaves the id in place. Click then sees the id as a short
    # option and fails — same as before our preprocessor existed in
    # that exact ordering. Users can work around by either putting
    # the id first, or using `--` explicitly.
    # This test pins current behavior: we intentionally don't try to
    # infer which flags take values (no Click-schema introspection in
    # a preprocessor) and accept the slight UX limitation.
    result = _preprocess_argv(["analyzetg", "analyze", "--all-flat", "-1003865481227"])
    # The negative number was NOT moved (prev was `--all-flat`).
    assert result == ["analyzetg", "analyze", "--all-flat", "-1003865481227"]


def test_option_value_pattern_not_rewritten() -> None:
    # `atg backfill --chat -1001234 --from-msg 5000` — the negative
    # number is the value of `--chat`, not a positional. Must stay
    # next to `--chat` so Click reads it as the option's value.
    assert _preprocess_argv(["atg", "backfill", "--chat", "-1001234", "--from-msg", "5000"]) == [
        "atg",
        "backfill",
        "--chat",
        "-1001234",
        "--from-msg",
        "5000",
    ]


def test_existing_double_dash_untouched() -> None:
    # User who already types `--` is explicit — respect their choice,
    # don't rewrite.
    assert _preprocess_argv(["analyzetg", "analyze", "--", "-1003865481227"]) == [
        "analyzetg",
        "analyze",
        "--",
        "-1003865481227",
    ]


def test_multiple_flags_before_and_after_id() -> None:
    # Real shape: `atg analyze -1003... --preset summary --last-days 7`.
    assert _preprocess_argv(
        ["atg", "analyze", "-1003865481227", "--preset", "summary", "--last-days", "7"]
    ) == [
        "atg",
        "analyze",
        "--preset",
        "summary",
        "--last-days",
        "7",
        "--",
        "-1003865481227",
    ]


def test_short_flag_not_confused_with_negative_number() -> None:
    # `-c` is the short form of `--console`; only `-<digits>` triggers
    # the rewrite.
    assert _preprocess_argv(["analyzetg", "analyze", "@foo", "-c"]) == [
        "analyzetg",
        "analyze",
        "@foo",
        "-c",
    ]


def test_chats_remove_regression() -> None:
    # Sibling regression: `atg chats remove -1001234 --purge-messages`.
    # Subcommand nesting (chats → remove) shouldn't matter — any
    # non-flag prev token is treated as non-option, so the id moves.
    assert _preprocess_argv(["atg", "chats", "remove", "-1001234", "--purge-messages"]) == [
        "atg",
        "chats",
        "remove",
        "--purge-messages",
        "--",
        "-1001234",
    ]


def test_empty_argv_is_safe() -> None:
    assert _preprocess_argv([]) == []
