"""Tests for analyzer.chunker."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from unread.analyzer import formatter as _formatter
from unread.analyzer.chunker import (
    _TRUNC_MARKER,
    _split_sentences,
    build_chunks,
    model_context_window,
)
from unread.models import Message


def _msg(i: int, text: str, date: datetime) -> Message:
    return Message(chat_id=1, msg_id=i, date=date, sender_name="alice", text=text)


@pytest.fixture
def disable_body_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift `formatter._BODY_CAP` so oversized-message tests reach the chunker.

    Normal-flow synthetic messages stay under the cap by construction,
    but the chunker's split path is what we want to exercise here — the
    cap would otherwise truncate the body before the chunker sees it.
    """
    monkeypatch.setattr(_formatter, "_BODY_CAP", 10_000_000)


def test_model_context_fallback() -> None:
    assert model_context_window("unknown-model") >= 8000


def test_single_chunk_when_budget_large() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    msgs = [_msg(i, f"m{i}", d + timedelta(seconds=i)) for i in range(20)]
    chunks = build_chunks(
        msgs,
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
    )
    assert len(chunks) == 1
    assert [m.msg_id for m in chunks[0].messages] == list(range(20))


def test_soft_break_on_long_pause() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    first = [_msg(i, "some meaningful text " * 100, d + timedelta(seconds=i)) for i in range(30)]
    # After a 2-hour gap, start new chunk even though budget allows it
    gap_start = d + timedelta(hours=3)
    second = [_msg(30 + i, "more text " * 100, gap_start + timedelta(seconds=i)) for i in range(5)]
    chunks = build_chunks(
        first + second,
        model="gpt-4o-mini",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
        soft_break_minutes=30,
        safety_margin=0,
    )
    assert len(chunks) >= 2


def test_all_messages_are_kept() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    msgs = [_msg(i, f"msg {i} " * 5, d + timedelta(seconds=i)) for i in range(50)]
    chunks = build_chunks(
        msgs,
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
    )
    seen = {m.msg_id for c in chunks for m in c.messages}
    assert seen == set(range(50))


# --- Task 3.3: split or truncate oversized single messages ----------------


def test_sentence_splitter_handles_en_punctuation() -> None:
    """The sentence regex should peel `.!?` boundaries followed by a
    capital letter or opening quote. Used by `_split_oversize` to
    bound where each split lands."""
    parts = _split_sentences('First. Second! Third? "Fourth" stays.')
    assert parts[0] == "First."
    assert "Second!" in parts
    assert "Third?" in parts
    # The closing fragment depends on regex behavior, but every
    # sentence must appear somewhere in the join.
    assert "Fourth" in " ".join(parts)


def test_sentence_splitter_handles_cyrillic() -> None:
    parts = _split_sentences("Раз. Два! Три? Четыре.")
    # All four uppercase-Cyrillic-headed sentences should be peeled apart.
    assert len(parts) >= 3


def test_oversized_single_message_is_split(disable_body_cap: None) -> None:
    """A single 200k-token message under a 32k chunk budget must be split
    into >=7 sub-chunks; sum of body lengths approximately equals the
    original (modulo trimmed whitespace at sentence boundaries)."""
    d = datetime(2026, 4, 1, 12, 0)
    # Build a long body of grammatically split sentences. Each sentence
    # is ~200 chars; ~40_000 sentences ≈ 8MB ≈ ~2M tokens (well past
    # the 32k cap so we'll see plenty of sub-chunks).
    sentence_pool = "Sentence number {n} brings news of significance. " * 8  # ~400 chars
    body = "".join(sentence_pool.format(n=i) for i in range(2000))  # ~800k chars
    big = _msg(1, body, d)
    chunks = build_chunks(
        [big],
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=2000,
        max_chunk_input_tokens=32_000,
        safety_margin=500,
    )
    # Each split message lives in its own chunk because it consumes the
    # whole budget; >=7 confirms we didn't bail with the old "emit one
    # oversize chunk" path.
    assert len(chunks) >= 7
    # Every sub-chunk preserves the original msg_id — citations resolve.
    for c in chunks:
        assert all(m.msg_id == 1 for m in c.messages)
    # Each chunk's tokens are under budget (otherwise we re-introduced
    # the bug where a single oversize message was emitted as-is).
    for c in chunks:
        assert c.tokens <= 32_000
    # Sub-message bodies recombine to roughly the original length.
    # Allow generous slack for whitespace normalization at boundaries.
    rejoined = " ".join(m.text or "" for c in chunks for m in c.messages)
    assert len(rejoined) >= int(len(body) * 0.85)


def test_oversized_single_sentence_is_truncated(disable_body_cap: None) -> None:
    """A single ~500k-token sentence with no `.!?` boundaries gets
    mid-sentence truncated, marker present.

    The body is a long unpunctuated sequence of varied ascii words so
    tiktoken's BPE doesn't degenerate (uniform inputs like `'x' * N`
    push tiktoken into a quadratic merge path that takes minutes per
    call). Real-world offenders are typically long URLs / base64 blobs
    / unbroken text walls, not a million repeated characters.
    """
    d = datetime(2026, 4, 1, 12, 0)
    # ~80k chars of varied ascii words separated by spaces — no `.!?`
    # so the sentence splitter sees a single sentence, well past the
    # body budget on a 4k-token chunk cap.
    word_pool = "alpha bravo charlie delta echo foxtrot golf hotel "
    body = (word_pool * 1500).strip()  # ~75k chars, ~10k tokens
    big = _msg(1, body, d)
    chunks = build_chunks(
        [big],
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=500,
        max_chunk_input_tokens=4_000,
        safety_margin=200,
    )
    # At least one rendered body carries the truncation marker.
    rendered = "\n".join(m.text or "" for c in chunks for m in c.messages)
    assert _TRUNC_MARKER in rendered
    # Truncated rendering still fits in the budget.
    for c in chunks:
        assert c.tokens <= 4_000


def test_normal_pair_packs_with_oversized_split(disable_body_cap: None) -> None:
    """Two normal messages + one oversized: chunker emits the normal pair
    in one chunk, splits the oversize message into its own chunks, with
    `(continued K/N)` markers visible on parts 2+."""
    d = datetime(2026, 4, 1, 12, 0)
    pre = _msg(1, "short hello.", d)
    sentence_pool = "Sentence number {n} carries some signal. " * 6
    big_body = "".join(sentence_pool.format(n=i) for i in range(1500))
    big = _msg(2, big_body, d + timedelta(seconds=1))
    post = _msg(3, "short goodbye.", d + timedelta(seconds=2))
    chunks = build_chunks(
        [pre, big, post],
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=2000,
        max_chunk_input_tokens=32_000,
        safety_margin=500,
    )
    # Render each chunk through format_messages to check what the
    # pipeline would actually ship to the model.
    from unread.analyzer.formatter import format_messages

    rendered_chunks = [format_messages(c.messages) for c in chunks]
    # At least one chunk renders the `(continued K/N)` marker.
    cont_marker_seen = any("(continued " in r for r in rendered_chunks)
    assert cont_marker_seen, "expected at least one continuation marker in rendered chunks"
    # First chunk holds the pre msg; last chunk holds the post msg.
    # (Exact packing depends on token math; assert presence rather than
    # position.)
    assert any("short hello." in r for r in rendered_chunks)
    assert any("short goodbye." in r for r in rendered_chunks)
    # The oversized msg_id must stay #2 across all sub-renderings.
    big_chunks = [c for c in chunks if any(m.msg_id == 2 for m in c.messages)]
    assert len(big_chunks) >= 2  # split into multiple parts


def test_split_preserves_metadata(disable_body_cap: None) -> None:
    """All sub-messages must keep the original sender, date, and msg_id
    so citations the model emits resolve to the same original."""
    d = datetime(2026, 4, 1, 12, 0)
    sentence_pool = "Sentence number {n} brings news of significance. " * 8
    body = "".join(sentence_pool.format(n=i) for i in range(2000))
    big = _msg(42, body, d)
    big.sender_name = "Charlie"
    chunks = build_chunks(
        [big],
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=2000,
        max_chunk_input_tokens=32_000,
        safety_margin=500,
    )
    all_subs = [m for c in chunks for m in c.messages]
    assert len(all_subs) >= 7
    for sub in all_subs:
        assert sub.msg_id == 42
        assert sub.sender_name == "Charlie"
        assert sub.date == d
        assert sub.chat_id == 1
