"""Tests for the wizard's tail-of-run "another preset?" loop.

Covers the pure arg-rewriter (`_build_followup_analyze_args`) and the
gate logic in `_run_another_preset_loop`. The picker UI itself runs
questionary, which is exercised by the higher-level wizard tests; here
we focus on the parts that don't need a TTY.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from unread import interactive
from unread.config import get_settings, reset_settings


def _baseline_args() -> dict[str, Any]:
    """Minimal kwargs dict matching what `build_analyze_args` produces.

    Includes period selectors (`last_hours`, `since`, `full_history`) so
    we can assert they get cleared by the follow-up builder.
    """
    return {
        "ref": "-1001234567890",
        "thread": None,
        "preset": "decisions",
        "last_days": None,
        "last_hours": 96,
        "full_history": False,
        "since": None,
        "until": None,
        "from_msg": None,
        "prompt_file": None,
        "model": None,
        "filter_model": None,
        "output": None,
        "console_out": False,
        "save_default": False,
        "mark_read": True,
        "no_cache": False,
        "include_transcripts": True,
        "min_msg_chars": None,
        "enrich": "voice,link",
        "enrich_all": False,
        "no_enrich": False,
        "yes": True,
        "all_flat": False,
        "all_per_topic": False,
        "with_comments": False,
        "comments_max": None,
        "comments_order": "all",
        "post_saved": False,
        "max_cost": None,
        "self_check": False,
        "cite_context": 0,
        "dry_run": False,
        "by": None,
        "post_to": None,
        "language": None,
        "report_language": None,
        "source_language": None,
    }


def test_followup_swaps_preset_and_clears_period() -> None:
    """The new preset wins; every period selector is cleared so
    `repeat_last` can re-load the absolute window from the DB."""
    args = _baseline_args()
    followup = interactive._build_followup_analyze_args(args, preset="summary")
    assert followup["preset"] == "summary"
    assert followup["repeat_last"] is True
    # Period selectors all cleared — no risk of "unread → nothing new" after
    # mark-read advanced the marker on the first run.
    for k in (
        "since",
        "until",
        "last_days",
        "last_hours",
        "last_minutes",
        "last_msgs",
        "from_msg",
        "msg",
    ):
        assert followup[k] is None, f"{k} not cleared"
    assert followup["full_history"] is False


def test_followup_forces_mark_read_false() -> None:
    """The first run advanced the marker; the second pass is read-only."""
    args = _baseline_args()  # mark_read=True (matches wizard default)
    followup = interactive._build_followup_analyze_args(args, preset="summary")
    assert followup["mark_read"] is False


def test_followup_preserves_unrelated_args() -> None:
    """Enrich, comments, output, language, etc. carry across so the
    follow-up runs with the same enrichment/output shape as the first."""
    args = _baseline_args()
    args["enrich"] = "voice,link"
    args["language"] = "ru"
    args["report_language"] = "ru"
    args["comments_order"] = "last"
    args["comments_max"] = 50
    followup = interactive._build_followup_analyze_args(args, preset="summary")
    assert followup["enrich"] == "voice,link"
    assert followup["language"] == "ru"
    assert followup["report_language"] == "ru"
    assert followup["comments_order"] == "last"
    assert followup["comments_max"] == 50


def test_followup_does_not_mutate_input_dict() -> None:
    """The builder returns a fresh dict — calling it twice on the same
    base args produces independent follow-up dicts."""
    args = _baseline_args()
    snapshot = dict(args)
    interactive._build_followup_analyze_args(args, preset="summary")
    assert args == snapshot


async def test_loop_gate_off_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """When `interactive.offer_more_presets=False`, the loop must not
    even open the preset picker — let alone call `cmd_analyze`."""
    reset_settings()
    settings = get_settings()
    monkeypatch.setattr(settings.interactive, "offer_more_presets", False)

    with (
        patch.object(interactive, "_pick_another_preset", new=AsyncMock()) as picker,
        patch("unread.analyzer.commands.cmd_analyze", new=AsyncMock()) as analyze,
    ):
        await interactive._run_another_preset_loop(_baseline_args(), first_preset="decisions")
        picker.assert_not_awaited()
        analyze.assert_not_awaited()


async def test_loop_gate_on_calls_picker_until_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate on → loop picks presets and re-runs `cmd_analyze` for each,
    accumulating "used" so the same preset is never re-picked."""
    reset_settings()
    settings = get_settings()
    monkeypatch.setattr(settings.interactive, "offer_more_presets", True)
    monkeypatch.setattr(interactive, "_can_show_followup_prompt", lambda: True)

    seen_used: list[set[str]] = []
    picks: list[Any] = ["summary", "tldr", interactive._PRESET_LOOP_DONE]

    async def fake_pick(used: set[str]):
        seen_used.append(set(used))
        return picks.pop(0)

    analyze_calls: list[dict[str, Any]] = []

    async def fake_analyze(**kwargs):
        analyze_calls.append(kwargs)

    with (
        patch.object(interactive, "_pick_another_preset", new=fake_pick),
        patch("unread.analyzer.commands.cmd_analyze", new=fake_analyze),
    ):
        await interactive._run_another_preset_loop(_baseline_args(), first_preset="decisions")

    # Two follow-up runs (summary, tldr); the Done sentinel ended the loop.
    assert [c["preset"] for c in analyze_calls] == ["summary", "tldr"]
    # The picker sees the running set of used presets, seeded with the
    # wizard's first preset and growing across iterations.
    assert seen_used == [
        {"decisions"},
        {"decisions", "summary"},
        {"decisions", "summary", "tldr"},
    ]
    # Every follow-up uses repeat_last + mark_read=False (sanity check
    # the loop wires the builder correctly, not just the picker).
    for c in analyze_calls:
        assert c["repeat_last"] is True
        assert c["mark_read"] is False


async def test_loop_esc_cancels_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Esc at the picker (returns None) exits the loop without calling
    `cmd_analyze`."""
    reset_settings()
    settings = get_settings()
    monkeypatch.setattr(settings.interactive, "offer_more_presets", True)
    monkeypatch.setattr(interactive, "_can_show_followup_prompt", lambda: True)

    with (
        patch.object(interactive, "_pick_another_preset", new=AsyncMock(return_value=None)),
        patch("unread.analyzer.commands.cmd_analyze", new=AsyncMock()) as analyze,
    ):
        await interactive._run_another_preset_loop(_baseline_args(), first_preset="decisions")
        analyze.assert_not_awaited()


async def test_loop_skipped_in_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate on but stdin/stdout aren't TTYs (the pytest / scripted case)
    → loop short-circuits. Without this guard `questionary` would raise
    EOFError trying to open raw-mode input."""
    reset_settings()
    settings = get_settings()
    monkeypatch.setattr(settings.interactive, "offer_more_presets", True)
    monkeypatch.setattr(interactive, "_can_show_followup_prompt", lambda: False)

    with (
        patch.object(interactive, "_pick_another_preset", new=AsyncMock()) as picker,
        patch("unread.analyzer.commands.cmd_analyze", new=AsyncMock()) as analyze,
    ):
        await interactive._run_another_preset_loop(_baseline_args(), first_preset="decisions")
        picker.assert_not_awaited()
        analyze.assert_not_awaited()
