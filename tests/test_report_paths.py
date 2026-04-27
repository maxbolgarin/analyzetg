"""Default report-path layout.

Forum topics live under their parent chat so `ls reports/<forum>/` shows
one directory per topic instead of a flat pile of `summary-<date>.md`
files that can't be told apart.

Untitled chats use `chat-<id>` so every such chat gets its own dir
instead of colliding on the old generic `chat/` slug.
"""

from __future__ import annotations

from pathlib import Path

from atg.analyzer.commands import (
    _chat_slug,
    _default_output_path,
    _slugify,
    _topic_slug,
    _unique_path,
)

# --- slug helpers ------------------------------------------------------


def test_chat_slug_uses_title_when_present():
    assert _chat_slug("UNION 3.0 | WORK GROUP", -1003865481227) == "union-3-0-work-group"


def test_chat_slug_falls_back_to_id_when_title_missing():
    assert _chat_slug(None, -1003865481227) == "chat-1003865481227"


def test_chat_slug_falls_back_to_id_when_title_slugs_empty():
    # Emoji-only / punctuation-only titles slug to ""; must not produce
    # an anonymous `/` directory.
    assert _chat_slug("!!!", 42) == "chat-42"
    assert _chat_slug("   ", 42) == "chat-42"


def test_topic_slug_uses_title_when_present():
    assert _topic_slug("AI hub", 2) == "ai-hub"


def test_topic_slug_falls_back_to_id_when_title_missing():
    assert _topic_slug(None, 2) == "topic-2"


# --- default path layout ----------------------------------------------


def test_default_path_non_forum():
    p = _default_output_path(
        chat_title="Bull Trading",
        chat_id=-100,
        preset="summary",
    )
    # No thread → no nested topic dir.
    assert p.parts[0] == "reports"
    assert p.parts[1] == "bull-trading"
    assert p.parts[2] == "analyze"
    assert p.name.startswith("summary-") and p.name.endswith(".md")


def test_default_path_forum_with_topic_title():
    p = _default_output_path(
        chat_title="UNION 3.0 | WORK GROUP",
        chat_id=-1003865481227,
        thread_id=2,
        thread_title="AI hub",
        preset="summary",
    )
    parts = p.parts
    # reports/<chat>/<topic>/analyze/<file>.md — four levels below 'reports'.
    assert parts[0] == "reports"
    assert parts[1] == "union-3-0-work-group"
    assert parts[2] == "ai-hub"
    assert parts[3] == "analyze"
    assert p.name.startswith("summary-") and p.name.endswith(".md")


def test_default_path_forum_without_topic_title_falls_back():
    # Direct `--thread 5` without a topic-list lookup: caller doesn't know
    # the title. The path must still separate this topic from others.
    p = _default_output_path(
        chat_title="Forum",
        chat_id=-100,
        thread_id=5,
        thread_title=None,
        preset="digest",
    )
    assert p.parts[2] == "topic-5"


def test_default_path_untitled_chat_never_produces_generic_chat():
    # The old layout collided every untitled chat onto `reports/chat/`.
    # This test pins that we never write `chat/` by itself anymore.
    p = _default_output_path(
        chat_title=None,
        chat_id=12345,
        preset="summary",
    )
    assert "chat-12345" in p.parts
    # Negative check — no bare `chat` component.
    assert "chat" not in p.parts


def test_default_path_thread_id_zero_treated_as_non_forum():
    # thread_id=0 is the sentinel for "no thread" (DB convention). Must
    # NOT produce a spurious topic-0 directory.
    p = _default_output_path(
        chat_title="Group",
        chat_id=-42,
        thread_id=0,
        preset="summary",
    )
    assert "topic-0" not in p.parts


# --- Unicode slugging ------------------------------------------------


def test_slugify_preserves_cyrillic():
    # The Cyrillic case is WHY we switched from `[A-Za-z0-9_-]` to `\w` —
    # ForumTopic titles like "ОБЩИЙ ЧАТ" used to slug to "" and force
    # every topic directory back to `topic-<id>`. Painful when you're
    # browsing reports by title.
    assert _slugify("ОБЩИЙ ЧАТ") == "общий-чат"
    assert _slugify("ТОРГОВЫЕ ИДЕИ/СЕТАПЫ") == "торговые-идеи-сетапы"
    assert _slugify("МАРАФОН") == "марафон"


def test_topic_slug_uses_cyrillic_title():
    assert _topic_slug("ОБЩИЙ ЧАТ", 1) == "общий-чат"


def test_chat_slug_mixed_latin_cyrillic():
    # "INV.NEXT | PRO | ЧАТ" — real chat title from user logs. Before:
    # everything after the ASCII run got stripped. After: keeps both.
    got = _chat_slug("INV.NEXT | PRO | ЧАТ", -1)
    assert got == "inv-next-pro-чат"


# --- Collision-safe unique_path -------------------------------------


def test_unique_path_returns_original_when_free(tmp_path: Path):
    target = tmp_path / "summary-2026-04-24_123045.md"
    assert _unique_path(target) == target


def test_unique_path_numbers_when_occupied(tmp_path: Path):
    target = tmp_path / "summary.md"
    target.write_text("existing")
    got = _unique_path(target)
    assert got.name == "summary-2.md"
    assert not got.exists()  # caller writes to it, we just picked the slot


def test_unique_path_finds_gap_after_multiple_collisions(tmp_path: Path):
    # Existing: summary.md, summary-2.md, summary-3.md → next free is -4.
    (tmp_path / "summary.md").write_text("a")
    (tmp_path / "summary-2.md").write_text("b")
    (tmp_path / "summary-3.md").write_text("c")
    got = _unique_path(tmp_path / "summary.md")
    assert got.name == "summary-4.md"


def test_default_path_stamp_includes_seconds():
    # Regression guard: stamp must be HH:MM:SS, not HH:MM — two reports
    # in the same minute with the same preset overwrote each other before.
    p = _default_output_path(chat_title="x", chat_id=1, preset="summary")
    # Filename shape: summary-YYYY-MM-DD_HHMMSS.md (15-char time-part before .md).
    stem = p.stem  # "summary-2026-04-24_123045"
    time_part = stem.rsplit("_", 1)[-1]
    assert len(time_part) == 6, f"expected HHMMSS, got {time_part!r}"
    assert time_part.isdigit()
