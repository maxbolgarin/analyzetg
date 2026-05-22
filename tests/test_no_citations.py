"""`analyze.no_citations`: strip `[#N](url)` citations from rendered + saved output.

Why this exists: some users want a plain-prose summary without the
em-dash citation cluster the LLM appends to every claim (`Foo bar —
[#5521](https://t.me/...), [#6318](https://t.me/...)`). The cached
LLM output is unaffected so toggling the setting doesn't bust the
analysis cache — only the displayed + saved copy changes.

Citations are NOT optional in the prompt today (every preset's base
rules require `[#<msg_id>](<link>)` for grounding). This is a strict
post-processing layer.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from unread.config import reset_settings

    monkeypatch.delenv("UNREAD_LOG_MODE", raising=False)
    reset_settings()
    yield
    reset_settings()


# ----- AnalyzeCfg field ------------------------------------------------


def test_no_citations_defaults_to_false():
    """Citations are on by default — they're how the report grounds claims."""
    from unread.config import AnalyzeCfg

    assert AnalyzeCfg().no_citations is False


def test_no_citations_accepts_true():
    from unread.config import AnalyzeCfg

    assert AnalyzeCfg(no_citations=True).no_citations is True


def test_no_citations_rejects_typos():
    """Strict-cfg: typos in config.toml must raise."""
    from unread.config import AnalyzeCfg

    with pytest.raises(ValidationError):
        AnalyzeCfg(no_citation=True)  # missing trailing 's'


# ----- _strip_citations helper ---------------------------------------


def test_strip_citations_removes_trailing_emdash_cluster():
    """The most common LLM shape: ` — [#N](url), [#N](url)` at end of bullet."""
    from unread.analyzer.commands import _strip_citations

    src = "Foo bar happened. — [#5521](https://t.me/c/123/5521), [#6318](https://t.me/c/123/6318)"
    assert _strip_citations(src) == "Foo bar happened."


def test_strip_citations_removes_single_trailing_cite():
    from unread.analyzer.commands import _strip_citations

    src = "Foo bar. — [#5521](https://t.me/c/123/5521)"
    assert _strip_citations(src) == "Foo bar."


def test_strip_citations_removes_inline_cite():
    from unread.analyzer.commands import _strip_citations

    src = "The [#5521](https://t.me/c/123/5521) message is key."
    out = _strip_citations(src)
    # No double-space leftover; either side of the citation collapsed cleanly.
    assert "[#5521]" not in out
    assert "https://t.me" not in out
    assert "  " not in out


def test_strip_citations_preserves_unrelated_markdown_links():
    """Only the `[#<digits>](url)` shape gets stripped. Regular markdown
    links (`[label](url)`) stay — they're not citations."""
    from unread.analyzer.commands import _strip_citations

    src = "See [the docs](https://example.com) for details. — [#5521](https://t.me/c/123/5521)"
    out = _strip_citations(src)
    assert "[the docs](https://example.com)" in out
    assert "[#5521]" not in out


def test_strip_citations_handles_multiline_body():
    from unread.analyzer.commands import _strip_citations

    src = (
        "## Главное\n\n"
        " • Foo bar. — [#5521](https://t.me/c/123/5521), [#6318](https://t.me/c/123/6318)\n"
        " • Baz qux. — [#7000](https://t.me/c/123/7000)\n"
    )
    out = _strip_citations(src)
    assert "[#5521]" not in out
    assert "[#6318]" not in out
    assert "[#7000]" not in out
    assert "Foo bar." in out
    assert "Baz qux." in out
    # Heading is untouched.
    assert "## Главное" in out


def test_strip_citations_idempotent_on_clean_text():
    """Running strip on already-clean text is a no-op."""
    from unread.analyzer.commands import _strip_citations

    src = "Foo bar.\n\n## Section\n\n - bullet"
    assert _strip_citations(src) == src


def test_strip_citations_no_dangling_whitespace_at_eol():
    """After stripping, lines must not end with spaces (would look messy
    in a saved markdown file)."""
    from unread.analyzer.commands import _strip_citations

    src = "Foo bar. — [#5521](https://t.me/c/123/5521)\nNext line."
    out = _strip_citations(src)
    for line in out.splitlines():
        assert line == line.rstrip(), f"trailing whitespace on {line!r}"


# ----- Section-aware preservation ------------------------------------


def test_strip_citations_preserves_worth_checking_section_ru():
    """`## Стоит посмотреть` is the summary preset's curated index of
    messages worth opening — its whole purpose IS the citation links.
    Strip must NOT touch this section, even when no_citations is on."""
    from unread.analyzer.commands import _strip_citations

    src = (
        "## TL;DR\n"
        "Неделя прошла. — [#100](https://t.me/c/1/100)\n"
        "\n"
        "## Главное\n"
        " - Foo. — [#101](https://t.me/c/1/101)\n"
        "\n"
        "## Стоит посмотреть\n"
        " - [#142639](https://t.me/c/1/142639) — разбор про дроны\n"
        " - [#142700](https://t.me/c/1/142700) — материал про Telegram\n"
    )
    out = _strip_citations(src)

    # Prose sections: stripped.
    assert "[#100]" not in out
    assert "[#101]" not in out
    # Preserved section: links kept.
    assert "[#142639]" in out
    assert "[#142700]" in out
    assert "t.me/c/1/142639" in out


def test_strip_citations_preserves_worth_checking_section_en():
    """English variant: `## Worth checking`."""
    from unread.analyzer.commands import _strip_citations

    src = (
        "## TL;DR\n"
        "The week unfolded. — [#100](https://t.me/c/1/100)\n"
        "\n"
        "## Worth checking\n"
        " - [#142639](https://t.me/c/1/142639) — drones deep-dive\n"
    )
    out = _strip_citations(src)
    assert "[#100]" not in out
    assert "[#142639]" in out


def test_strip_citations_preserves_highlights_key_insights():
    """`highlights` preset: `## Key insights` / `## Основные инсайты` is
    the numbered list of link-anchored takeaways."""
    from unread.analyzer.commands import _strip_citations

    src_en = (
        "## Key insights\n"
        "1. **First.** Context here. — @alice, [#123](https://t.me/c/1/123)\n"
        "2. **Second.** Body. — @bob, [#456](https://t.me/c/1/456)\n"
    )
    out_en = _strip_citations(src_en)
    assert "[#123]" in out_en
    assert "[#456]" in out_en

    src_ru = "## Основные инсайты\n1. **Тезис.** Контекст. — @автор, [#789](https://t.me/c/1/789)\n"
    out_ru = _strip_citations(src_ru)
    assert "[#789]" in out_ru


def test_strip_citations_preserves_links_section():
    """`links` preset: `## Links` / `## Ссылки` — the trailing
    `, [#12345](link)` author-credit citation stays inside this section.
    """
    from unread.analyzer.commands import _strip_citations

    src = (
        "## Ссылки\n"
        "\n"
        "### Дроны\n"
        " - **[Repo](https://example.com)** — описание. — @автор, [#900](https://t.me/c/1/900)\n"
    )
    out = _strip_citations(src)
    assert "[#900]" in out
    assert "t.me/c/1/900" in out


def test_strip_citations_section_state_resets_on_next_heading():
    """After a preserved section, the next `##` heading must reset
    state — citations in subsequent prose sections still get stripped."""
    from unread.analyzer.commands import _strip_citations

    src = (
        "## Worth checking\n"
        " - [#100](https://t.me/c/1/100) — important\n"
        "\n"
        "## Decisions\n"
        " - Decided X. — [#200](https://t.me/c/1/200)\n"
    )
    out = _strip_citations(src)
    # Preserved.
    assert "[#100]" in out
    # State reset, prose section stripped.
    assert "[#200]" not in out
    assert "Decided X." in out


def test_strip_citations_unknown_heading_strips_inside():
    """A heading we don't recognize is treated as prose — strip applies
    inside. This is the safe default: opt-in preserve list, not opt-out."""
    from unread.analyzer.commands import _strip_citations

    src = "## Some Random Section\n - bullet. — [#500](https://t.me/c/1/500)\n"
    out = _strip_citations(src)
    assert "[#500]" not in out
    assert " - bullet." in out


# ----- Override-key wiring -------------------------------------------


def test_no_citations_in_override_keys():
    from unread.db._keys import OVERRIDE_KEYS

    assert "analyze.no_citations" in OVERRIDE_KEYS


def test_apply_one_override_sets_no_citations():
    from unread.config import get_settings
    from unread.db.repo import _apply_one_override

    s = get_settings()
    s.analyze.no_citations = False
    _apply_one_override(s, "analyze.no_citations", "1")
    assert s.analyze.no_citations is True

    _apply_one_override(s, "analyze.no_citations", "0")
    assert s.analyze.no_citations is False


def test_apply_one_override_ignores_garbage_no_citations():
    from unread.config import get_settings
    from unread.db.repo import _apply_one_override

    s = get_settings()
    s.analyze.no_citations = False
    _apply_one_override(s, "analyze.no_citations", "maybe")
    assert s.analyze.no_citations is False


# ----- Settings UI presence ------------------------------------------


def test_no_citations_in_settings_registry():
    """`unread settings` (interactive editor) must surface the toggle so
    users can flip it without editing config.toml."""
    from unread.settings.commands import _BY_KEY

    sd = _BY_KEY.get("analyze.no_citations")
    assert sd is not None
    assert sd.kind == "bool"
    assert sd.category_key == "settings_cat_analyze"
    # Label + desc i18n entries exist (lookup raises if missing).
    assert sd.label
    assert sd.desc


# ----- _print_and_write integration ---------------------------------


def test_print_and_write_strips_citations_when_setting_on(tmp_path):
    """End-to-end: with `analyze.no_citations=True`, the saved markdown
    file contains no `[#N](url)` patterns and no t.me/c URLs."""
    from unread.analyzer.commands import _print_and_write
    from unread.analyzer.pipeline import AnalysisResult
    from unread.config import get_settings

    body = (
        "## TL;DR\n\n"
        "Trading idea worked out. — [#5521](https://t.me/c/123/5521), "
        "[#6318](https://t.me/c/123/6318)\n\n"
        "Another point. — [#7000](https://t.me/c/123/7000)\n"
    )
    result = AnalysisResult(
        preset="summary",
        model="gpt-5.4-mini",
        chat_id=-123,
        thread_id=0,
        msg_count=10,
        chunk_count=1,
        batch_hashes=[],
        final_result=body,
        total_cost_usd=0.0,
        cache_hits=0,
        cache_misses=1,
    )

    s = get_settings()
    s.analyze.no_citations = True

    out_path = tmp_path / "report.md"
    _print_and_write(
        result,
        output=out_path,
        title="Test chat",
        console_out=False,  # skip terminal render — we only check the saved file
    )

    saved = out_path.read_text(encoding="utf-8")
    # No markdown citation markers, no t.me URLs left behind.
    assert "[#5521]" not in saved
    assert "[#6318]" not in saved
    assert "[#7000]" not in saved
    assert "t.me/c/" not in saved
    # The prose itself survives.
    assert "Trading idea worked out." in saved
    assert "Another point." in saved
    # And the heading.
    assert "## TL;DR" in saved


def test_print_and_write_keeps_citations_by_default(tmp_path):
    """Default (`no_citations=False`): saved file keeps `[#N](url)`."""
    from unread.analyzer.commands import _print_and_write
    from unread.analyzer.pipeline import AnalysisResult
    from unread.config import get_settings

    body = "Trading idea worked out. — [#5521](https://t.me/c/123/5521)"
    result = AnalysisResult(
        preset="summary",
        model="gpt-5.4-mini",
        chat_id=-123,
        thread_id=0,
        msg_count=10,
        chunk_count=1,
        batch_hashes=[],
        final_result=body,
        total_cost_usd=0.0,
        cache_hits=0,
        cache_misses=1,
    )

    s = get_settings()
    s.analyze.no_citations = False

    out_path = tmp_path / "report.md"
    _print_and_write(
        result,
        output=out_path,
        title="Test chat",
        console_out=False,
    )

    saved = out_path.read_text(encoding="utf-8")
    assert "[#5521]" in saved
    assert "t.me/c/123/5521" in saved
