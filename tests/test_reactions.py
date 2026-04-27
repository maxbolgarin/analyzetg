"""Tests for reactions: Telethon extraction, repo round-trip, formatter tag."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from atg.analyzer.formatter import format_messages
from atg.db.repo import Repo
from atg.models import Message
from atg.tg.sync import detect_reactions


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _reaction_count(emoticon: str | None, count: int, document_id: int | None = None):
    reaction = SimpleNamespace(emoticon=emoticon, document_id=document_id)
    return SimpleNamespace(reaction=reaction, count=count)


# --- detect_reactions ---------------------------------------------------


def test_detect_reactions_none_when_missing() -> None:
    msg = SimpleNamespace(reactions=None)
    assert detect_reactions(msg) is None


def test_detect_reactions_returns_none_for_empty_results() -> None:
    msg = SimpleNamespace(reactions=SimpleNamespace(results=[]))
    assert detect_reactions(msg) is None


def test_detect_reactions_standard_emoji() -> None:
    msg = SimpleNamespace(
        reactions=SimpleNamespace(
            results=[
                _reaction_count("👍", 18),
                _reaction_count("❤️", 1),
                _reaction_count("🔥", 2),
            ]
        )
    )
    assert detect_reactions(msg) == {"👍": 18, "❤️": 1, "🔥": 2}


def test_detect_reactions_custom_emoji_uses_doc_id() -> None:
    msg = SimpleNamespace(reactions=SimpleNamespace(results=[_reaction_count(None, 3, document_id=123456)]))
    assert detect_reactions(msg) == {"custom:123456": 3}


def test_detect_reactions_skips_zero_and_invalid() -> None:
    msg = SimpleNamespace(
        reactions=SimpleNamespace(
            results=[
                _reaction_count("👍", 0),  # skip
                _reaction_count(None, 5, document_id=None),  # skip (no id)
                _reaction_count("✅", 2),
            ]
        )
    )
    assert detect_reactions(msg) == {"✅": 2}


# --- formatter tag ------------------------------------------------------


def _m(msg_id: int, **kw) -> Message:
    base = {
        "chat_id": 1,
        "msg_id": msg_id,
        "date": datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        "sender_name": "alice",
        "text": "body",
    }
    base.update(kw)
    return Message(**base)


def test_formatter_renders_reactions_tag() -> None:
    m = _m(1, reactions={"👍": 18, "❤️": 1, "🔥": 2})
    out = format_messages([m])
    # Sorted by count desc: thumbs-up first, then fire, then heart.
    assert "[reactions: 👍×18 🔥×2 ❤️×1]" in out


def test_formatter_skips_single_reaction() -> None:
    # A lone reaction is noise, not signal — tag must be absent.
    m = _m(1, reactions={"👍": 1})
    out = format_messages([m])
    assert "reactions:" not in out


def test_formatter_folds_custom_emoji_into_plus_n() -> None:
    m = _m(1, reactions={"👍": 3, "custom:111": 2, "custom:222": 1})
    out = format_messages([m])
    assert "[reactions: 👍×3 +3 custom]" in out


def test_formatter_handles_none_reactions() -> None:
    m = _m(1, reactions=None)
    out = format_messages([m])
    assert "reactions:" not in out


# --- repo round-trip ----------------------------------------------------


async def test_repo_round_trip_preserves_reactions(repo: Repo) -> None:
    m = Message(
        chat_id=42,
        msg_id=100,
        date=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        sender_name="bob",
        text="hi",
        reactions={"👍": 5, "🔥": 2},
    )
    await repo.upsert_messages([m])
    loaded = await repo.iter_messages(chat_id=42)
    assert len(loaded) == 1
    assert loaded[0].reactions == {"👍": 5, "🔥": 2}


async def test_repo_round_trip_none_stays_none(repo: Repo) -> None:
    m = Message(
        chat_id=42,
        msg_id=101,
        date=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        sender_name="bob",
        text="hi",
    )
    await repo.upsert_messages([m])
    loaded = await repo.iter_messages(chat_id=42)
    assert loaded[0].reactions is None


async def test_repo_upsert_overwrites_reactions(repo: Repo) -> None:
    base_kw = {
        "chat_id": 42,
        "msg_id": 200,
        "date": datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        "sender_name": "bob",
        "text": "hi",
    }
    await repo.upsert_messages([Message(**base_kw, reactions={"👍": 1})])
    await repo.upsert_messages([Message(**base_kw, reactions={"👍": 5, "❤️": 1})])
    loaded = await repo.iter_messages(chat_id=42)
    assert loaded[0].reactions == {"👍": 5, "❤️": 1}
