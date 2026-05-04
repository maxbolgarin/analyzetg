"""Tests for `unread/core/paths.py` — slug helpers, date parsing, window math.

Pre-prod gap: paths.py shipped with no dedicated tests. These pin the
behavior of helpers consumed throughout the pipeline (compute_window
in cli.py, derive_internal_id in core/pipeline.py, slugify in the
report-path builders).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unread.core.paths import (
    assert_under_reports,
    chat_slug,
    compute_window,
    derive_internal_id,
    parse_ymd,
    reports_dir,
    slugify,
    topic_slug,
    unique_path,
)

# ---- slugify --------------------------------------------------------------


def test_slugify_lowercases_and_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_preserves_unicode_letters():
    """Cyrillic / CJK / Arabic must survive — Telegram chats often have
    non-ASCII titles and we don't want to lose them in the directory name."""
    assert slugify("Привет Мир") == "привет-мир"
    assert "中文" in slugify("中文 chat")


def test_slugify_caps_at_40_chars():
    long = "a" * 200
    assert len(slugify(long)) == 40


def test_slugify_empty_or_punctuation_only_returns_empty():
    """Caller (`chat_slug`) must provide a fallback when slugify returns ''."""
    assert slugify("") == ""
    assert slugify("!!!") == ""
    assert slugify("---") == ""


def test_slugify_collapses_runs_of_non_word_chars():
    """Multiple non-word chars in a row collapse to one dash."""
    assert slugify("hello!!!world") == "hello-world"
    assert slugify("hello   world") == "hello-world"


# ---- chat_slug / topic_slug ----------------------------------------------


def test_chat_slug_uses_title_when_present():
    assert chat_slug("My Chat", chat_id=-100123) == "my-chat"


def test_chat_slug_falls_back_to_chat_id_when_title_empty():
    assert chat_slug(None, chat_id=-100123) == "chat-100123"
    assert chat_slug("", chat_id=-100123) == "chat-100123"


def test_chat_slug_uses_abs_value_for_negative_chat_id():
    """Telegram channel IDs are negative; the slug uses abs() so the
    directory name doesn't have a leading dash."""
    assert chat_slug("", chat_id=-1001234567890) == "chat-1001234567890"


def test_topic_slug_uses_title_when_present():
    assert topic_slug("General Discussion", thread_id=42) == "general-discussion"


def test_topic_slug_falls_back_to_thread_id_when_title_empty():
    assert topic_slug(None, thread_id=42) == "topic-42"
    assert topic_slug("", thread_id=42) == "topic-42"


# ---- derive_internal_id ---------------------------------------------------


def test_derive_internal_id_strips_telegram_channel_prefix():
    """Telethon channel/supergroup IDs use the `-100<n>` shape; the
    `t.me/c/<n>/...` link form needs the bare <n>."""
    assert derive_internal_id(-1001234567890) == 1234567890


def test_derive_internal_id_returns_none_for_users_and_small_groups():
    """Positive IDs (DMs / users) and shallow negative IDs (small groups)
    don't have a t.me/c/ link form."""
    assert derive_internal_id(123456789) is None  # user / positive
    assert derive_internal_id(0) is None
    assert derive_internal_id(-12345) is None  # small basic group


def test_derive_internal_id_handles_minimum_threshold():
    """Boundary: 1_000_000_000_001 is the smallest valid channel ID."""
    assert derive_internal_id(-1_000_000_000_001) == 1


# ---- parse_ymd -----------------------------------------------------------


def test_parse_ymd_returns_utc_aware_datetime():
    out = parse_ymd("2026-05-04")
    assert out is not None
    assert out.tzinfo is UTC
    assert out.year == 2026 and out.month == 5 and out.day == 4
    # Midnight, not local-time-noon
    assert out.hour == 0 and out.minute == 0


def test_parse_ymd_returns_none_for_empty_input():
    assert parse_ymd(None) is None
    assert parse_ymd("") is None


def test_parse_ymd_raises_on_malformed_input():
    with pytest.raises(ValueError):
        parse_ymd("not-a-date")
    with pytest.raises(ValueError):
        parse_ymd("2026/05/04")  # wrong separator


# ---- compute_window -------------------------------------------------------


def test_compute_window_last_minutes_takes_precedence():
    since, until = compute_window(
        since="2020-01-01", until="2020-12-31", last_days=30, last_hours=2, last_minutes=15
    )
    # last_minutes wins
    assert (until - since).total_seconds() == pytest.approx(15 * 60, rel=0.01)
    assert until.tzinfo is UTC


def test_compute_window_last_hours_overrides_last_days():
    since, until = compute_window(since=None, until=None, last_days=7, last_hours=3)
    assert (until - since).total_seconds() == pytest.approx(3 * 3600, rel=0.01)


def test_compute_window_last_days_uses_now_minus_n_days():
    since, until = compute_window(since=None, until=None, last_days=30)
    now = datetime.now(UTC)
    # within a small slop
    assert abs((until - now).total_seconds()) < 5
    assert (until - since) == timedelta(days=30)


def test_compute_window_falls_back_to_explicit_since_until():
    since, until = compute_window(since="2026-01-01", until="2026-02-01", last_days=None)
    assert since == datetime(2026, 1, 1, tzinfo=UTC)
    assert until == datetime(2026, 2, 1, tzinfo=UTC)


def test_compute_window_returns_none_pair_when_nothing_supplied():
    assert compute_window(since=None, until=None, last_days=None) == (None, None)


# ---- unique_path ----------------------------------------------------------


def test_unique_path_returns_input_when_unused(tmp_path):
    p = tmp_path / "report.md"
    assert unique_path(p) == p


def test_unique_path_appends_dash_2_when_taken(tmp_path):
    p = tmp_path / "report.md"
    p.write_text("first")
    assert unique_path(p).name == "report-2.md"


def test_unique_path_walks_up_to_first_free_slot(tmp_path):
    p = tmp_path / "report.md"
    p.write_text("0")
    (tmp_path / "report-2.md").write_text("0")
    (tmp_path / "report-3.md").write_text("0")
    assert unique_path(p).name == "report-4.md"


# ---- assert_under_reports -------------------------------------------------


def test_assert_under_reports_accepts_path_under_reports_dir():
    p = reports_dir() / "youtube" / "channel" / "video.md"
    out = assert_under_reports(p)
    # Returns the input path unchanged so callers can still
    # round-trip with `path.relative_to(reports_dir())`.
    assert out == p


def test_assert_under_reports_rejects_traversal():
    # An attacker-controlled slug that contains `..` must not let the
    # implicit-default path escape `reports_dir()`. We don't expect this
    # via the slugify pipeline today, but the guard is the load-bearing
    # invariant when a future caller skips slugify.
    p = reports_dir() / ".." / ".." / "etc" / "passwd"
    with pytest.raises(ValueError, match="escapes reports dir"):
        assert_under_reports(p)


def test_assert_under_reports_rejects_absolute_outside():
    with pytest.raises(ValueError, match="escapes reports dir"):
        assert_under_reports(reports_dir().parent / "outside.md")
