"""Tests for unread.youtube.citations — citation timestamp back-shift."""

from __future__ import annotations

from unread.youtube.citations import shift_citation_timestamps

_BASE = "https://www.youtube.com/watch?v=abc123"


def test_shift_subtracts_seconds_from_url() -> None:
    src = f"quote [08:15]({_BASE}&t=495s)"
    out = shift_citation_timestamps(src)
    assert "t=490s" in out
    assert "t=495s" not in out


def test_shift_rewrites_clock_label() -> None:
    src = f"[08:15]({_BASE}&t=495s)"
    out = shift_citation_timestamps(src)
    assert "[08:10]" in out
    assert "[08:15]" not in out


def test_shift_preserves_label_with_hours() -> None:
    src = f"[01:23:45]({_BASE}&t=5025s)"
    out = shift_citation_timestamps(src)
    assert "[01:23:40]" in out
    assert "t=5020s" in out


def test_shift_clamps_at_zero() -> None:
    src = f"[00:02]({_BASE}&t=2s)"
    out = shift_citation_timestamps(src)
    assert "t=0s" in out
    assert "[00:00]" in out


def test_shift_skips_non_clock_label() -> None:
    """`#N` segment labels stay put — only the URL shifts."""
    src = f"[#754]({_BASE}&t=754s)"
    out = shift_citation_timestamps(src)
    assert "[#754]" in out
    assert "t=749s" in out


def test_shift_skips_free_text_label() -> None:
    src = f"[in the intro]({_BASE}&t=120s)"
    out = shift_citation_timestamps(src)
    assert "[in the intro]" in out
    assert "t=115s" in out


def test_shift_label_no_match_leaves_label() -> None:
    """Label `08:15` but URL `t=999s` — they disagree, so don't rewrite the label."""
    src = f"[08:15]({_BASE}&t=999s)"
    out = shift_citation_timestamps(src)
    assert "t=994s" in out
    assert "[08:15]" in out


def test_shift_handles_short_url_form() -> None:
    """`?t=Ns` (t is the first/only query param) — youtu.be links."""
    src = "[01:00](https://youtu.be/abc?t=60s)"
    out = shift_citation_timestamps(src)
    assert "t=55s" in out


def test_shift_zero_offset_is_noop() -> None:
    src = f"[08:15]({_BASE}&t=495s)"
    assert shift_citation_timestamps(src, offset_sec=0) == src


def test_shift_negative_offset_is_noop() -> None:
    src = f"[08:15]({_BASE}&t=495s)"
    assert shift_citation_timestamps(src, offset_sec=-5) == src


def test_shift_empty_input() -> None:
    assert shift_citation_timestamps("") == ""


def test_shift_multiple_citations() -> None:
    src = f"[01:00]({_BASE}&t=60s) and [02:00]({_BASE}&t=120s)"
    out = shift_citation_timestamps(src)
    assert "[00:55]" in out
    assert "[01:55]" in out
    assert "t=55s" in out
    assert "t=115s" in out


def test_shift_custom_offset() -> None:
    src = f"[01:00]({_BASE}&t=60s)"
    out = shift_citation_timestamps(src, offset_sec=10)
    assert "[00:50]" in out
    assert "t=50s" in out


def test_shift_ignores_url_without_t_param() -> None:
    src = f"[link]({_BASE})"
    assert shift_citation_timestamps(src) == src


def test_shift_handles_t_param_in_middle() -> None:
    src = f"[09:00]({_BASE}&t=540s&list=foo)"
    out = shift_citation_timestamps(src)
    assert "t=535s" in out
    assert "&list=foo" in out


def test_shift_preserves_non_youtube_text() -> None:
    src = f"Setup is in [08:15]({_BASE}&t=495s); see also section 3."
    out = shift_citation_timestamps(src)
    assert "Setup is in" in out
    assert "see also section 3." in out
