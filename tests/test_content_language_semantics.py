"""Pin the new semantics: `content_language` drives prompts/LLM input;
`language` only drives UI / report headings.

Test scenarios mirror the user's stated use case: English UI but a
Russian-content chat → presets must come from `presets/ru/`, but the
saved-report `## Sources` heading must still be English.
"""

from __future__ import annotations

import pytest

from analyzetg.analyzer import prompts
from analyzetg.analyzer.commands import _expand_citations
from analyzetg.analyzer.pipeline import AnalysisOptions, _load_preset
from analyzetg.db.repo import Repo


def test_load_preset_reads_from_content_language_directory():
    """`_load_preset(opts, language=...)` is given `content_language` by
    pipeline.run_analysis. Picks the RU tree for content_language='ru'."""
    opts = AnalysisOptions(preset="summary")
    preset_ru = _load_preset(opts, language="ru")
    preset_en = _load_preset(opts, language="en")
    # Bodies differ between languages — same name, different prose.
    assert preset_ru.system != preset_en.system
    # RU body unmistakably contains Cyrillic; EN body does not.
    assert any(0x0400 <= ord(ch) <= 0x04FF for ch in preset_ru.system)
    assert not any(0x0400 <= ord(ch) <= 0x04FF for ch in preset_en.system)


def test_compose_system_prompt_loads_preset_dir_by_language():
    """compose_system_prompt's `language` is the content/prompt language —
    callers (pipeline) pass `content_language` to it."""
    en = prompts.compose_system_prompt("PRESET", topic_titles=None, language="en")
    ru = prompts.compose_system_prompt("PRESET", topic_titles=None, language="ru")
    # Each gets its own _base.md (post v6: source-neutral wording).
    assert "You analyze a stream of messages" in en
    assert "Ты — аналитик потока сообщений" in ru


@pytest.mark.asyncio
async def test_sources_heading_uses_language_not_content_language(tmp_path) -> None:
    """`_expand_citations` heading is user-facing → driven by `language`,
    NOT `content_language`. EN UI + RU content must still produce '## Sources'."""
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        from datetime import UTC, datetime

        from analyzetg.models import Message

        now = datetime.now(UTC)
        msgs = [Message(chat_id=1, msg_id=i, date=now, text=f"m{i}", sender_name="x") for i in range(1, 6)]
        await repo.upsert_messages(msgs)

        body = "Cite [#3](https://t.me/x/3)."
        # Mixed: UI = en, content = ru. Heading should follow `language` (en).
        out = await _expand_citations(body, chat_id=1, repo=repo, context_n=1, language="en")
        assert "## Sources" in out
        assert "## Источники" not in out
    finally:
        await repo.close()


def test_options_payload_distinguishes_language_and_content_language():
    """`content_language` (LLM-input language) must be in `options_payload`
    so flipping it busts the cache — different content_language means a
    different prompts tree and different LLM output. UI `language` is
    intentionally NOT in the payload (it doesn't reach the LLM)."""
    from analyzetg.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    s.locale.language = "en"
    s.locale.content_language = "en"
    try:
        opts = AnalysisOptions(preset="digest")
        p_en = opts.options_payload(_load_preset(opts, language="en"))
        s.locale.content_language = "ru"
        p_ru = opts.options_payload(_load_preset(opts, language="ru"))
    finally:
        reset_settings()
    assert "language" not in p_en  # UI language is intentionally absent
    assert p_en["content_language"] == "en"
    assert p_ru["content_language"] == "ru"
    assert p_en != p_ru


def test_base_version_bumped_to_v5_or_later():
    """Locale-semantics flip is a structural change — BASE_VERSION must
    advance so legacy v4 cache rows are not served."""
    assert prompts.BASE_VERSION >= "v5"
