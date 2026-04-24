"""Tests for the interactive wizard's pure arg-builders."""

from __future__ import annotations

from analyzetg import interactive
from analyzetg.interactive import InteractiveAnswers, build_analyze_args, build_dump_args


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


def test_wizard_always_sets_yes_true() -> None:
    # The wizard already asks "Run it?" via questionary. Every invocation
    # of cmd_analyze that comes through the wizard must pass yes=True so
    # downstream _run_forum_per_topic / _run_no_ref skip their own
    # typer.confirm prompts. A stuck terminal on that second prompt was
    # the originating bug.
    kw = build_analyze_args(_answers())
    assert kw["yes"] is True

    kw2 = build_analyze_args(_answers(forum_all_per_topic=True, chat_kind="forum"))
    assert kw2["yes"] is True


def test_from_msg_period_passes_raw_ref_string() -> None:
    # User picks "From a specific message" and enters a bare msg_id. The
    # string flows through unchanged — cmd_analyze re-parses it (same code
    # path as --from-msg on the CLI).
    kw = build_analyze_args(_answers(period="from_msg", custom_from_msg="12345"))
    assert kw["from_msg"] == "12345"
    # Shouldn't mix with any other period flag.
    assert kw["last_days"] is None
    assert kw["full_history"] is False
    assert kw["since"] is None and kw["until"] is None


def test_from_msg_period_passes_link_through() -> None:
    # Telegram message link — cmd_analyze's _parse_from_msg handles this
    # via tg.links.parse. The wizard's job is just to collect + forward.
    link = "https://t.me/c/1234567890/890"
    kw = build_analyze_args(_answers(period="from_msg", custom_from_msg=link))
    assert kw["from_msg"] == link


def test_non_from_msg_periods_leave_from_msg_none() -> None:
    # Regression guard: any other period key must keep from_msg=None so
    # cmd_analyze's precedence rules fire correctly.
    for period in ("unread", "last7", "last30", "full", "custom"):
        kw = build_analyze_args(_answers(period=period, custom_from_msg="stale-value"))
        assert kw["from_msg"] is None, f"period={period!r} leaked from_msg"


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


# --- build_dump_args (new enrichment passthrough) ---------------------


def _dump_kwargs() -> dict:
    return {"fmt": "md", "with_transcribe": False, "include_transcripts": True}


def test_dump_default_enrich_is_config_defaults() -> None:
    # enrich_kinds=None (wizard not used / skipped) → cmd_dump sees
    # enrich=None and no_enrich=False, which build_enrich_opts then
    # resolves to config defaults.
    kw = build_dump_args(_answers(), **_dump_kwargs())
    assert kw["enrich"] is None
    assert kw["enrich_all"] is False
    assert kw["no_enrich"] is False


def test_dump_explicit_empty_enrich_becomes_no_enrich() -> None:
    # User opened the wizard's enrich step and unchecked everything.
    # That intent should flow to cmd_dump as --no-enrich so config
    # defaults don't quietly re-enable voice/videonote/link.
    kw = build_dump_args(_answers(enrich_kinds=[]), **_dump_kwargs())
    assert kw["enrich"] is None
    assert kw["no_enrich"] is True


def test_dump_populated_enrich_becomes_csv() -> None:
    # Explicit selection: wizard → comma-separated string → cmd_dump
    # parses it and runs exactly those kinds. Order preserves insertion.
    kw = build_dump_args(_answers(enrich_kinds=["voice", "image", "link"]), **_dump_kwargs())
    assert kw["enrich"] == "voice,image,link"
    assert kw["no_enrich"] is False


async def test_dump_all_unread_wizard_skips_second_confirm_and_forwards_enrich(monkeypatch) -> None:
    answers = _answers(run_on_all_unread=True, enrich_kinds=["image"], mark_read=True)
    captured = {}

    async def fake_collect_answers(**kwargs):
        return answers

    async def fake_run_all_unread_dump(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(interactive, "_collect_answers", fake_collect_answers)
    monkeypatch.setattr("analyzetg.export.commands.run_all_unread_dump", fake_run_all_unread_dump)

    await interactive.run_interactive_dump(fmt="md", with_transcribe=False, include_transcripts=True)

    assert captured["yes"] is True
    assert captured["enrich"] == "image"
    assert captured["mark_read"] is True
