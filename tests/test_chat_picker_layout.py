"""Chat-picker table layout.

The picker is interactive (questionary → prompt_toolkit) so there's no
good way to drive it end-to-end in unit tests. But the row-rendering is
pure — pin the column alignment + short-kind mapping so a future tweak
to widths / labels doesn't silently wreck the layout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from analyzetg.interactive import (
    _COL_DATE,
    _COL_KIND,
    _COL_UNREAD,
    _chat_header_row,
    _chat_row,
    _fmt_count,
    _fmt_date,
    _short_kind,
)


def test_short_kind_folds_supergroup_to_group():
    # Users don't care about supergroup vs group distinction in this picker.
    assert _short_kind("supergroup") == "group"
    assert _short_kind("group") == "group"


def test_short_kind_preserves_others():
    assert _short_kind("channel") == "channel"
    assert _short_kind("forum") == "forum"
    assert _short_kind("user") == "user"


def test_fmt_count_right_aligns_to_column_width():
    # Leading spaces must add up to exactly _COL_UNREAD chars.
    s = _fmt_count(42)
    assert len(s) == _COL_UNREAD
    assert s.strip() == "42"


def test_fmt_count_zero_shows_dash():
    s = _fmt_count(0)
    assert len(s) == _COL_UNREAD
    assert s.strip() == "—"


def test_fmt_date_today_is_hhmm():
    now = datetime.now()
    s = _fmt_date(now)
    # HH:MM — 5 chars, one colon.
    assert len(s) == 5
    assert s.count(":") == 1


def test_fmt_date_this_year_shows_month_day():
    last_week = datetime.now() - timedelta(days=7)
    s = _fmt_date(last_week)
    # "MMM DD HH:MM" shape — 12 chars, two separators (space + colon).
    assert len(s) == 12
    assert " " in s and ":" in s


def test_fmt_date_none_is_single_dash():
    # Caller pads to column width; the helper itself returns a bare "—".
    assert _fmt_date(None) == "—"


def test_fmt_date_aware_utc_renders_in_local_time():
    # Regression guard: Telethon returns tz-aware UTC datetimes and the
    # picker used to strftime them as-is, so a user in UTC+2 seeing a
    # message Telegram displayed at 15:47 would see it as 13:47 in the
    # picker. Fix: astimezone() before formatting. Here we build a UTC
    # datetime "now - 30 min" and check the rendered HH:MM matches what
    # system-local time would produce, not UTC.

    now_utc = datetime.now(UTC)
    half_hour_ago = now_utc - timedelta(minutes=30)
    got = _fmt_date(half_hour_ago)
    # Must be HH:MM (today path) — no "Apr DD" prefix.
    assert len(got) == 5 and got.count(":") == 1
    # And the hour must match the LOCAL hour of `half_hour_ago`, not UTC.
    local_expected = half_hour_ago.astimezone().strftime("%H:%M")
    assert got == local_expected


def test_fmt_date_naive_datetime_passes_through():
    # Naive datetimes (no tzinfo) are assumed to already be local and
    # rendered verbatim — don't break callers that pass local wall-clock
    # times straight in.
    now = datetime.now()
    an_hour_ago = now - timedelta(hours=1)
    assert _fmt_date(an_hour_ago) == an_hour_ago.strftime("%H:%M")


def test_chat_row_aligns_fixed_columns():
    # Two rows with different unread counts and kinds must line up at the
    # title column — the core reason we use fixed widths.
    a = _chat_row(unread=1435, kind="forum", last_msg_date=None, title="Foo")
    b = _chat_row(unread=9, kind="supergroup", last_msg_date=None, title="Bar")
    title_offset_a = a.index("Foo")
    title_offset_b = b.index("Bar")
    assert title_offset_a == title_offset_b


def test_chat_row_does_not_use_dot_separators():
    # We replaced the dotted `·` separators with padded whitespace; guard
    # against a regression that reintroduces them.
    row = _chat_row(unread=10, kind="forum", last_msg_date=None, title="X")
    assert "·" not in row


def test_chat_header_aligns_with_row():
    # Header and a sample row must share the same column offsets so the
    # header labels line up over their values. We check the offset of
    # `title` in the header matches the offset of the title string in a row.
    header = _chat_header_row()
    row = _chat_row(unread=1, kind="forum", last_msg_date=None, title="TITLE_MARKER")
    # In the header, the title column starts where "title" begins.
    hdr_title_pos = header.index("title")
    row_title_pos = row.index("TITLE_MARKER")
    assert hdr_title_pos == row_title_pos


def test_chat_row_supergroup_displayed_as_group():
    # End-to-end: supergroup folds through the row renderer.
    row = _chat_row(unread=1, kind="supergroup", last_msg_date=None, title="X")
    assert " group  " in row  # surrounded by column padding
    assert "supergroup" not in row


def test_column_widths_fit_expected_content():
    # Defensive: if someone shrinks a column below the longest expected
    # value, headers / rows mangle. Sanity-check the constants against
    # the strings they need to hold.
    assert len("channel") <= _COL_KIND
    assert len("Apr 23 09:14") <= _COL_DATE
    assert len("unread") <= _COL_UNREAD
