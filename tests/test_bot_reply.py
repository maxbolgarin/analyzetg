"""Tests for `unread.bot.reply` — small-report shortcut.

Focused on the pure-logic part (`_is_small_report`). Full send-flow
integration is covered by manual smoke tests against a live bot.
"""

from __future__ import annotations

from unread.bot.reply import _SMALL_REPORT_THRESHOLD_CHARS, _is_small_report


def test_is_small_report_returns_true_for_short_markdown():
    short = "# Summary\n\n## TL;DR\n\nOne paragraph.\n\n## Sources\n\n- foo\n"
    assert len(short) < _SMALL_REPORT_THRESHOLD_CHARS
    assert _is_small_report(short) is True


def test_is_small_report_returns_false_for_long_markdown():
    body = "## Section\n\n" + ("Lorem ipsum dolor sit amet. " * 200)
    assert len(body) >= _SMALL_REPORT_THRESHOLD_CHARS
    assert _is_small_report(body) is False


def test_is_small_report_boundary_one_below_threshold():
    """Exactly threshold-1 chars must still be considered small."""
    text = "x" * (_SMALL_REPORT_THRESHOLD_CHARS - 1)
    assert _is_small_report(text) is True


def test_is_small_report_boundary_at_threshold():
    """At threshold exactly = not small (gets the PDF)."""
    text = "x" * _SMALL_REPORT_THRESHOLD_CHARS
    assert _is_small_report(text) is False


def test_is_small_report_empty_string_is_small():
    assert _is_small_report("") is True
