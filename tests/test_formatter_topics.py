"""Topic-aware formatter: flat-forum output groups messages by topic.

These pin the contract we rely on in all-flat forum analysis: each
topic gets its own header, order is predictable, and the `topic_titles=None`
default is byte-identical to today's code (so every non-forum path keeps
producing the same bytes — and the analysis-cache key stays stable).
"""

from __future__ import annotations

from datetime import datetime

from analyzetg.analyzer.formatter import chat_header_preamble, format_messages
from analyzetg.models import Message


def _msg(msg_id: int, thread_id: int | None, text: str, minute: int) -> Message:
    return Message(
        chat_id=-1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, minute),
        thread_id=thread_id,
        sender_name="Alice",
        text=text,
    )


def _three_topics() -> tuple[list[Message], dict[int, str]]:
    # Messages interleaved across three topics to simulate a real forum.
    msgs = [
        _msg(100, 1, "общий чат msg 1", 0),
        _msg(101, 44511, "идеи msg 1", 1),
        _msg(102, 1, "общий чат msg 2", 2),
        _msg(103, 58776, "марафон msg 1", 3),
        _msg(104, 44511, "идеи msg 2", 4),
    ]
    titles = {
        1: "ОБЩИЙ ЧАТ",
        44511: "ТОРГОВЫЕ ИДЕИ",
        58776: "МАРАФОН",
    }
    return msgs, titles


def test_format_messages_groups_by_topic_when_titles_provided():
    msgs, titles = _three_topics()
    out = format_messages(msgs, topic_titles=titles)

    # Every group's header appears exactly once (English labels, EN default).
    assert out.count("=== Topic: ОБЩИЙ ЧАТ (id=1) ===") == 1
    assert out.count("=== Topic: ТОРГОВЫЕ ИДЕИ (id=44511) ===") == 1
    assert out.count("=== Topic: МАРАФОН (id=58776) ===") == 1

    # Group order is by first-message date: topic 1 (min 0) → topic 44511
    # (min 1) → topic 58776 (min 3). We use index-of-header as a cheap proxy.
    i1 = out.index("ОБЩИЙ ЧАТ")
    i2 = out.index("ТОРГОВЫЕ ИДЕИ")
    i3 = out.index("МАРАФОН")
    assert i1 < i2 < i3


def test_format_messages_preserves_within_topic_chronology():
    msgs, titles = _three_topics()
    out = format_messages(msgs, topic_titles=titles)
    # Within topic 1, msg 100 (12:00) must come before msg 102 (12:02).
    i100 = out.index("общий чат msg 1")
    i102 = out.index("общий чат msg 2")
    assert i100 < i102


def test_format_messages_falls_back_to_topic_id_for_unknown_thread():
    # A message with thread_id=999 (deleted topic, not in titles map)
    # must still get a header — just the numeric fallback.
    msgs = [
        _msg(10, 1, "known", 0),
        _msg(11, 999, "unknown", 1),
    ]
    titles = {1: "A"}
    out = format_messages(msgs, topic_titles=titles)
    assert "=== Topic: A (id=1) ===" in out
    # Unknown thread → fallback `#<id>` for the name.
    assert "=== Topic: #999 (id=999) ===" in out


def test_format_messages_without_titles_is_byte_identical():
    # The most critical regression guard: every non-forum path calls
    # format_messages without topic_titles. That output must be exactly
    # what it was before the feature landed, because the analysis cache
    # key is the hash of preset+prompt+msg_ids+options — the formatted
    # prompt text influences the OpenAI-side prompt cache too.
    msgs, _ = _three_topics()

    ungrouped = format_messages(msgs)
    # Should have NO topic header and NO blank-line group separators
    # introduced by the grouped path.
    assert "=== Topic:" not in ungrouped
    # Messages remain in input (chronological) order.
    i100 = ungrouped.index("общий чат msg 1")
    i101 = ungrouped.index("идеи msg 1")
    i102 = ungrouped.index("общий чат msg 2")
    assert i100 < i101 < i102


def test_format_messages_empty_topic_titles_behaves_like_none():
    # An empty dict is treated identically to None — otherwise a caller
    # with an empty forum (no topics fetched yet) would get an empty
    # grouping pass that subtly changes line breaks.
    msgs, _ = _three_topics()
    assert format_messages(msgs, topic_titles={}) == format_messages(msgs)


# --- chat_header_preamble ---------------------------------------------


def test_preamble_includes_forum_line_when_titles_given():
    titles = {1: "Topic A", 2: "Topic B", 3: "Topic C"}
    out = chat_header_preamble("My Forum", None, topic_titles=titles)
    assert "=== Chat: My Forum ===" in out
    assert "Forum: 3 topic(s)" in out
    assert "Topic A" in out and "Topic B" in out and "Topic C" in out


def test_preamble_truncates_large_topic_list():
    # 12 topics → first 8 listed, then "and 4 more".
    titles = {i: f"T{i}" for i in range(1, 13)}
    out = chat_header_preamble("Forum", None, topic_titles=titles)
    assert "Forum: 12 topic(s)" in out
    assert "and 4 more" in out
    # T1..T8 should appear; T9..T12 should not (truncated).
    for i in range(1, 9):
        assert f"T{i}" in out
    for i in range(9, 13):
        # Avoid false matches like T10 containing T1; guard with word boundary
        # via explicit token wrapping. Here the preamble joins with ", ".
        assert f", T{i}" not in out
        assert not out.endswith(f"T{i}")


def test_preamble_omits_forum_line_when_titles_none_or_empty():
    # Regression guard symmetric to format_messages — non-forum paths
    # must NOT gain a spurious "Forum: …" line in the static prefix.
    assert "Forum:" not in chat_header_preamble("Chat", None)
    assert "Forum:" not in chat_header_preamble("Chat", None, topic_titles={})
