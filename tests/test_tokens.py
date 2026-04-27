"""Regression tests for `atg.util.tokens` — the chunker depends on
accurate token counts, so silent regressions here directly translate into
wrong chunk sizes and truncations at runtime."""

from __future__ import annotations

from atg.util.tokens import count_message_tokens, count_tokens


def test_count_tokens_empty_string_is_zero() -> None:
    assert count_tokens("") == 0


def test_count_tokens_nonzero_for_ascii_text() -> None:
    n = count_tokens("Hello, world!")
    assert n > 0
    # An upper bound keeps us honest if tokenizer swaps silently.
    assert n < 10


def test_count_tokens_longer_text_has_more_tokens() -> None:
    short = count_tokens("Hi.")
    long = count_tokens("Hi. " * 50)
    assert long > short


def test_count_tokens_handles_unicode() -> None:
    # Russian tokenizes denser than ASCII — shouldn't crash or return 0.
    n = count_tokens("Привет, мир!")
    assert n > 0


def test_count_tokens_unknown_model_falls_back() -> None:
    # Unknown model → falls back to o200k_base; must not raise.
    assert count_tokens("hello", model="gpt-imaginary-999") > 0


def test_count_message_tokens_adds_overhead() -> None:
    msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi."},
    ]
    total = count_message_tokens(msgs)
    # Must be more than raw content alone (overhead: 4 per message + 2 trailer).
    raw = (
        count_tokens("system") + count_tokens("You are helpful.") + count_tokens("user") + count_tokens("Hi.")
    )
    assert total >= raw + 4 * 2 + 2


def test_count_message_tokens_empty_list_is_just_trailer() -> None:
    # Documented overhead: + 2 trailer tokens regardless of messages.
    assert count_message_tokens([]) == 2


def test_count_message_tokens_missing_fields_safe() -> None:
    # Defensive: if a caller hands us a message without content/role, don't crash.
    assert count_message_tokens([{"role": "user"}]) >= 4
    assert count_message_tokens([{}]) >= 4
