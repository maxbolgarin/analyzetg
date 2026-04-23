"""Tests for the interactive wizard's pure arg-builder."""

from __future__ import annotations

from analyzetg.interactive import InteractiveAnswers, build_analyze_args


def _answers(**overrides) -> InteractiveAnswers:
    defaults: dict = {
        "chat_ref": "@somegroup",
        "chat_kind": "supergroup",
        "thread_id": None,
        "forum_all_flat": False,
        "forum_all_per_topic": False,
        "preset": "summary",
        "period": "unread",
        "custom_since": None,
        "custom_until": None,
        "console_out": False,
        "mark_read": False,
    }
    defaults.update(overrides)
    return InteractiveAnswers(**defaults)


def test_unread_default_leaves_period_flags_empty() -> None:
    kw = build_analyze_args(_answers())
    assert kw["last_days"] is None
    assert kw["since"] is None and kw["until"] is None
    assert kw["full_history"] is False
    assert kw["ref"] == "@somegroup"
    assert kw["preset"] == "summary"


def test_last7_sets_last_days() -> None:
    kw = build_analyze_args(_answers(period="last7"))
    assert kw["last_days"] == 7
    assert kw["full_history"] is False
    assert kw["since"] is None


def test_full_period_sets_full_history() -> None:
    kw = build_analyze_args(_answers(period="full"))
    assert kw["full_history"] is True
    assert kw["last_days"] is None


def test_custom_period_passes_since_until() -> None:
    kw = build_analyze_args(_answers(period="custom", custom_since="2026-04-01", custom_until="2026-04-20"))
    assert kw["since"] == "2026-04-01"
    assert kw["until"] == "2026-04-20"
    assert kw["full_history"] is False
    assert kw["last_days"] is None


def test_forum_thread_selection() -> None:
    kw = build_analyze_args(_answers(thread_id=42, chat_kind="forum"))
    assert kw["thread"] == 42
    assert kw["all_flat"] is False
    assert kw["all_per_topic"] is False


def test_forum_per_topic_flag() -> None:
    kw = build_analyze_args(_answers(forum_all_per_topic=True, chat_kind="forum", thread_id=None))
    assert kw["thread"] is None
    assert kw["all_per_topic"] is True
    assert kw["all_flat"] is False


def test_forum_flat_flag_with_last_days() -> None:
    kw = build_analyze_args(_answers(forum_all_flat=True, chat_kind="forum", period="last7"))
    assert kw["all_flat"] is True
    assert kw["last_days"] == 7


def test_console_and_mark_read_flags() -> None:
    kw = build_analyze_args(_answers(console_out=True, mark_read=True))
    assert kw["console_out"] is True
    assert kw["mark_read"] is True


def test_run_on_all_unread_field_defaults_false() -> None:
    # Exists on the dataclass (used by the wizard to dispatch to the batch path).
    assert _answers().run_on_all_unread is False
    a = _answers()
    a.run_on_all_unread = True
    assert a.run_on_all_unread is True
