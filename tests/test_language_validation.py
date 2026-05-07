"""Coverage for the language catalog, picker pools, preset fallback,
and CLI flag validation introduced when we widened report/source/audio
language pickers beyond the en/ru i18n pool.
"""

from __future__ import annotations

import pytest
import typer

from unread.util.languages import (
    ISO_639_1,
    POPULAR_CODES,
    WHISPER_LANGUAGES,
    is_valid_language_code,
    language_display_name,
    normalize_language_code,
)

# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def test_iso_catalog_size():
    """ISO 639-1 has ~184 codes — anything substantially smaller is a bug."""
    assert 180 <= len(ISO_639_1) <= 200


def test_popular_codes_subset_of_iso():
    """Popular shortlist must reference real ISO 639-1 codes."""
    assert set(POPULAR_CODES).issubset(set(ISO_639_1.keys()))


def test_popular_codes_unique_and_lowercase():
    assert len(set(POPULAR_CODES)) == len(POPULAR_CODES)
    assert all(c == c.lower() and len(c) == 2 for c in POPULAR_CODES)


def test_popular_starts_with_en_ru():
    """The two i18n-translated languages lead the popular pool."""
    assert POPULAR_CODES[0] == "en"
    assert POPULAR_CODES[1] == "ru"


def test_whisper_subset_of_iso():
    """Every documented Whisper language is a valid ISO 639-1 code."""
    assert WHISPER_LANGUAGES.issubset(set(ISO_639_1.keys()))


# ---------------------------------------------------------------------------
# normalize_language_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pt", "pt"),
        ("PT", "pt"),
        ("Pt", "pt"),
        ("  en ", "en"),
        ("pt-BR", "pt"),
        ("zh_Hans", "zh"),
        ("Portuguese", "pt"),
        ("portuguese", "pt"),
        ("  scottish gaelic ", "gd"),
        ("Russian", "ru"),
    ],
)
def test_normalize_accepts(raw, expected):
    assert normalize_language_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "klingon",
        "xx",
        "zz",
        "english pirate",
        "1",
        "p",
        "ptbr",
    ],
)
def test_normalize_rejects(raw):
    assert normalize_language_code(raw) is None


def test_is_valid_wrapper():
    assert is_valid_language_code("pt") is True
    assert is_valid_language_code("klingon") is False


def test_language_display_name():
    assert language_display_name("pt") == "Portuguese"
    assert language_display_name("zz") == "Zz"  # title-cased fallback
    assert language_display_name("") == ""


# ---------------------------------------------------------------------------
# Picker pools
# ---------------------------------------------------------------------------


def test_supported_ui_languages_is_strict():
    """UI pool only includes languages with both i18n + presets — today en/ru."""
    from unread.settings.commands import _supported_ui_languages

    pool = _supported_ui_languages()
    assert pool[0] == "en"
    assert "ru" in pool
    # Must not include languages that have no i18n entries.
    assert "pt" not in pool
    assert "zh" not in pool


def test_supported_llm_languages_is_wider():
    """LLM-output pool is the popular shortlist — must include pt, zh, etc."""
    from unread.settings.commands import _supported_llm_languages

    pool = _supported_llm_languages()
    assert "pt" in pool
    assert "zh" in pool
    assert pool[0] == "en"
    assert len(pool) >= 20


def test_supported_audio_languages_filters_by_whisper():
    """Audio pool is intersection of popular + Whisper-supported."""
    from unread.settings.commands import _supported_audio_languages

    pool = _supported_audio_languages()
    assert all(c in WHISPER_LANGUAGES for c in pool)
    assert "en" in pool


# ---------------------------------------------------------------------------
# Preset fallback
# ---------------------------------------------------------------------------


def test_get_presets_falls_back_to_en_for_unknown_language():
    """`get_presets("pt")` must not raise; falls back to presets/en/."""
    from unread.analyzer.prompts import clear_preset_cache, get_presets

    clear_preset_cache()
    en = get_presets("en")
    pt = get_presets("pt")
    # Same preset set as English — fallback returned the en dict.
    assert set(pt.keys()) == set(en.keys())


def test_get_presets_caches_fallback_under_requested_key():
    """Second call for the same fallback language must hit the cache (no warning, same dict)."""
    from unread.analyzer.prompts import clear_preset_cache, get_presets

    clear_preset_cache()
    first = get_presets("ja")
    second = get_presets("ja")
    assert first is second


def test_get_presets_raises_for_missing_en_install(tmp_path, monkeypatch):
    """If presets/en/ is missing, the fallback chain bottoms out as a hard error."""
    from unread.analyzer import prompts

    fake_dir = tmp_path / "presets"
    fake_dir.mkdir()
    monkeypatch.setattr(prompts, "PRESETS_DIR", fake_dir)
    prompts.clear_preset_cache()
    with pytest.raises(RuntimeError, match="Preset directory not found for language 'en'"):
        prompts.get_presets("en")
    # And a non-en request should also bubble up the en-missing error.
    prompts.clear_preset_cache()
    with pytest.raises(RuntimeError, match="Preset directory not found"):
        prompts.get_presets("pt")


# ---------------------------------------------------------------------------
# CLI flag validation
# ---------------------------------------------------------------------------


def test_cli_validate_lang_flags_accepts_canonical_codes():
    from unread.cli import _validate_lang_flags

    assert _validate_lang_flags("en", "pt", "ja") == ("en", "pt", "ja")


def test_cli_validate_lang_flags_normalizes():
    from unread.cli import _validate_lang_flags

    # Title-cased English name + locale-tagged code → both normalised.
    assert _validate_lang_flags(None, "PT-BR", "Portuguese") == (None, "pt", "pt")


def test_cli_validate_lang_flags_passthrough_empty():
    from unread.cli import _validate_lang_flags

    assert _validate_lang_flags(None, None, None) == (None, None, None)
    assert _validate_lang_flags(None, "", None) == (None, "", None)


def test_cli_validate_rejects_garbage_report_language():
    from unread.cli import _validate_lang_flags

    with pytest.raises(typer.BadParameter, match="--report-language"):
        _validate_lang_flags(None, "klingon", None)


def test_cli_validate_rejects_garbage_content_language():
    from unread.cli import _validate_lang_flags

    with pytest.raises(typer.BadParameter, match="--content-language"):
        _validate_lang_flags(None, None, "klingon")


def test_cli_validate_rejects_unsupported_ui_language():
    """--language is held to the strict UI pool (en/ru today)."""
    from unread.cli import _validate_lang_flags

    with pytest.raises(typer.BadParameter, match="UI language"):
        _validate_lang_flags("pt", None, None)


def test_cli_validate_rejects_garbage_ui_language():
    from unread.cli import _validate_lang_flags

    with pytest.raises(typer.BadParameter, match="--language"):
        _validate_lang_flags("klingon", None, None)
