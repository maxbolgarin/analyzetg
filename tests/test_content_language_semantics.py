"""Pin the new three-axis semantics:

* `locale.language` — UI / saved-report headings only. Drives `i18n.t()`.
* `locale.report_language` — what the LLM writes the analysis in. Picks
  the `presets/<lang>/` tree; falls back to `language` when empty.
* `locale.content_language` — Whisper-style source-content language hint.
  Empty (the default) means the LLM auto-detects from the source text.

Test scenarios mirror the user's stated use case: English UI but a
Russian-content chat → presets must come from `presets/ru/`, but the
saved-report `## Sources` heading must still be English.
"""

from __future__ import annotations

import pytest

from unread.analyzer import prompts
from unread.analyzer.commands import _expand_citations
from unread.analyzer.pipeline import AnalysisOptions, _load_preset
from unread.db.repo import Repo


def test_load_preset_reads_from_report_language_directory():
    """`_load_preset(opts, language=...)` is given the report language by
    `pipeline.run_analysis`. Picks the RU tree for report_language='ru'."""
    opts = AnalysisOptions(preset="summary")
    preset_ru = _load_preset(opts, language="ru")
    preset_en = _load_preset(opts, language="en")
    # Bodies differ between languages — same name, different prose.
    assert preset_ru.system != preset_en.system
    # RU body unmistakably contains Cyrillic; EN body does not.
    assert any(0x0400 <= ord(ch) <= 0x04FF for ch in preset_ru.system)
    assert not any(0x0400 <= ord(ch) <= 0x04FF for ch in preset_en.system)


def test_compose_system_prompt_loads_preset_dir_by_language():
    """compose_system_prompt's `language` is the report/prompt language —
    callers (pipeline) pass the resolved report language to it."""
    en = prompts.compose_system_prompt("PRESET", topic_titles=None, language="en")
    ru = prompts.compose_system_prompt("PRESET", topic_titles=None, language="ru")
    # Each gets its own _base.md (post v6: source-neutral wording).
    assert "You analyze a stream of messages" in en
    assert "Ты — аналитик потока сообщений" in ru


def test_compose_system_prompt_emits_generic_enforcement_when_hint_unset():
    """Even when no Whisper-style source hint is set, the system prompt
    now carries a generic language-enforcement directive that tells the
    LLM to (a) detect the source language itself and (b) write the
    analysis in the report language regardless. This is the fallback
    that catches cases where URL-based source detection didn't fire."""
    out = prompts.compose_system_prompt("PRESET", language="ru", source_language="")
    # Generic directive names the report language explicitly.
    assert "OUTPUT LANGUAGE: write the analysis in `ru`" in out
    # And tells the model to keep the two languages distinct.
    assert "two separate things" in out or "MUST stay distinct" in out
    # The "strong + named" branch must NOT fire — we don't know the
    # source language yet, so we can't name it.
    assert "non-negotiable" not in out.lower()


def test_compose_system_prompt_injects_source_language_when_set():
    """When the user explicitly sets `locale.content_language` AND it
    differs from `report_language`, the system prompt gets a directive
    explicitly forbidding the model from mirroring the source language
    in the analysis body. (When the two languages match, a softer
    informational line is used instead.)"""
    out = prompts.compose_system_prompt("PRESET", language="ru", source_language="zh")
    # Both languages are named explicitly so the model can't get
    # confused about which is which.
    assert "source content is in `zh`" in out
    assert "MUST be in `ru`" in out
    # And the anti-mirror directive is the load-bearing line — must be
    # present in some form.
    assert "non-negotiable" in out.lower()


def test_compose_system_prompt_softer_hint_when_languages_match():
    """When source and report languages match, the model needs no
    anti-mirror directive — just a verbatim-quotation reminder. Pin
    that the heavy 'MUST be in X' wording is suppressed in this case
    to keep prompt-cache friendliness."""
    out = prompts.compose_system_prompt("PRESET", language="ru", source_language="ru")
    assert "Quote spans verbatim in `ru`" in out
    assert "non-negotiable" not in out.lower()


def test_compose_system_prompt_normalizes_source_language_whitespace():
    """Leading/trailing whitespace in the hint should be stripped before
    injection so a stray space doesn't break cache hashes downstream."""
    spaced = prompts.compose_system_prompt("PRESET", language="en", source_language="  zh  ")
    clean = prompts.compose_system_prompt("PRESET", language="en", source_language="zh")
    assert spaced == clean


@pytest.mark.asyncio
async def test_sources_heading_uses_language_not_report_language(tmp_path) -> None:
    """`_expand_citations` heading is user-facing → driven by `language`,
    NOT `report_language`. EN UI + RU report content must still produce
    '## Sources' (the heading the *user* reads)."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        from datetime import UTC, datetime

        from unread.models import Message

        now = datetime.now(UTC)
        msgs = [Message(chat_id=1, msg_id=i, date=now, text=f"m{i}", sender_name="x") for i in range(1, 6)]
        await repo.upsert_messages(msgs)

        body = "Cite [#3](https://t.me/x/3)."
        # Mixed: UI = en, report = ru. Heading should follow `language` (en).
        out = await _expand_citations(body, chat_id=1, repo=repo, context_n=1, language="en")
        assert "## Sources" in out
        assert "## Источники" not in out
    finally:
        await repo.close()


def test_options_payload_distinguishes_language_and_report_language():
    """`report_language` (LLM-output language) is what enters the cache
    payload (under the legacy `content_language` key, for back-compat).
    Flipping it busts cache. UI `language` is intentionally NOT in the
    payload (it doesn't reach the LLM)."""
    from unread.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    s.locale.language = "en"
    s.locale.report_language = "en"
    try:
        opts = AnalysisOptions(preset="digest")
        p_en = opts.options_payload(_load_preset(opts, language="en"))
        s.locale.report_language = "ru"
        p_ru = opts.options_payload(_load_preset(opts, language="ru"))
    finally:
        reset_settings()
    assert "language" not in p_en  # UI language is intentionally absent
    assert p_en["content_language"] == "en"  # legacy key, value = report lang
    assert p_ru["content_language"] == "ru"
    assert p_en != p_ru


def test_options_payload_emits_source_language_only_when_set():
    """The new `source_language` cache key is conditionally emitted so
    users who never opt into the source-hint don't lose existing cache
    rows on upgrade."""
    from unread.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    s.locale.language = "ru"
    s.locale.report_language = "ru"
    s.locale.content_language = ""  # default — no hint
    try:
        opts = AnalysisOptions(preset="digest")
        no_hint = opts.options_payload(_load_preset(opts, language="ru"))
        s.locale.content_language = "zh"
        with_hint = opts.options_payload(_load_preset(opts, language="ru"))
    finally:
        reset_settings()
    assert "source_language" not in no_hint
    assert with_hint["source_language"] == "zh"
    assert no_hint != with_hint


def test_base_version_bumped_for_three_axis_split():
    """Three-axis split is a structural prompt change (`_base.md` rewrite
    + new optional source-language line) — BASE_VERSION must advance so
    legacy cache rows are not served."""
    # v8 is the bump that introduced the new wording + source_language kwarg.
    assert prompts.BASE_VERSION >= "v8"
