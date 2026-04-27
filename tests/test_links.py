"""Tests for unread.tg.links — spec §6.1 table coverage."""

from __future__ import annotations

import pytest

from unread.tg.links import parse


def test_username_bare() -> None:
    p = parse("@durov")
    assert p.kind == "username"
    assert p.username == "durov"
    assert p.msg_id is None


def test_username_without_at() -> None:
    p = parse("durov")
    assert p.kind == "username"
    assert p.username == "durov"


def test_public_link_no_post() -> None:
    p = parse("https://t.me/durov")
    assert p.kind == "username"
    assert p.username == "durov"
    assert p.msg_id is None


def test_public_post_link() -> None:
    p = parse("https://t.me/durov/123")
    assert p.kind == "username"
    assert p.username == "durov"
    assert p.msg_id == 123


def test_public_topic_post_link() -> None:
    p = parse("https://t.me/somegroup/100/5000")
    assert p.kind == "username"
    assert p.username == "somegroup"
    assert p.thread_id == 100
    assert p.msg_id == 5000


def test_private_link_post() -> None:
    p = parse("https://t.me/c/1234567890/5000")
    assert p.kind == "internal_id"
    assert p.chat_id == -1001234567890
    assert p.msg_id == 5000
    assert p.thread_id is None


def test_private_link_topic_post() -> None:
    p = parse("https://t.me/c/1234567890/100/5000")
    assert p.kind == "internal_id"
    assert p.chat_id == -1001234567890
    assert p.thread_id == 100
    assert p.msg_id == 5000


def test_invite_plus() -> None:
    p = parse("https://t.me/+AbCdEf_Gh1")
    assert p.kind == "invite"
    assert p.invite_hash == "AbCdEf_Gh1"


def test_invite_joinchat() -> None:
    p = parse("https://t.me/joinchat/AbCdEf_Gh1")
    assert p.kind == "invite"
    assert p.invite_hash == "AbCdEf_Gh1"


def test_tg_protocol() -> None:
    p = parse("tg://resolve?domain=durov&post=42")
    assert p.kind == "username"
    assert p.username == "durov"
    assert p.msg_id == 42


def test_numeric_id_with_prefix() -> None:
    p = parse("-1001234567890")
    assert p.kind == "numeric_id"
    assert p.chat_id == -1001234567890


def test_numeric_id_plain() -> None:
    p = parse("123456")
    assert p.kind == "numeric_id"
    assert p.chat_id == 123456


def test_numeric_id_missing_minus_on_channel_is_auto_fixed() -> None:
    # User pastes the id without the minus — we auto-prepend it for the
    # canonical -100xxxxxxxxxx form.
    p = parse("1003865481227")
    assert p.kind == "numeric_id"
    assert p.chat_id == -1003865481227


def test_small_positive_numeric_id_stays_positive() -> None:
    # Plain user ids / small group ids aren't channels — don't touch them.
    p = parse("12345678")
    assert p.chat_id == 12345678


def test_self_marker() -> None:
    assert parse("me").kind == "self"
    assert parse("@me").kind == "self"


def test_fuzzy_fallback() -> None:
    p = parse("Bull Trading")
    assert p.kind == "fuzzy"
    assert p.raw == "Bull Trading"


@pytest.mark.parametrize("s", ["https://example.com/foo", "https://t.me/", ""])
def test_non_matching_urls_fall_to_fuzzy(s: str) -> None:
    p = parse(s)
    # Empty string and unknown URLs default to fuzzy
    assert p.kind in {"fuzzy"}
