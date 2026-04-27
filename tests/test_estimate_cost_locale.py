"""`estimate_cost` reads `settings.locale.content_language` to vary
`AVG_TOKENS_PER_MSG` between RU (60) and EN (40)."""

from __future__ import annotations

from unread.analyzer import pipeline as pipeline_mod
from unread.analyzer.pipeline import _avg_tokens_per_msg, estimate_cost
from unread.analyzer.prompts import get_presets
from unread.config import get_settings, reset_settings


def test_avg_tokens_per_msg_is_language_keyed():
    assert _avg_tokens_per_msg("ru") == 60
    assert _avg_tokens_per_msg("en") == 40
    # Fallback for autodetect / empty / unknown.
    assert _avg_tokens_per_msg("") == 50
    assert _avg_tokens_per_msg(None) == 50
    assert _avg_tokens_per_msg("xx") == 50


def test_estimate_cost_changes_with_content_language():
    """A Cyrillic-heavy chat estimates more tokens (and dollars) per message
    than an English chat under the same preset."""
    reset_settings()
    s = get_settings()
    preset = get_presets("en")["digest"]
    s.locale.content_language = "en"
    lo_en, hi_en = estimate_cost(n_messages=500, preset=preset, settings=s)
    s.locale.content_language = "ru"
    lo_ru, hi_ru = estimate_cost(n_messages=500, preset=preset, settings=s)
    reset_settings()
    # Pricing may be missing → both None; if present, RU > EN reliably.
    if lo_en is None or lo_ru is None:
        return
    assert lo_ru > lo_en
    assert hi_ru > hi_en


def test_avg_tokens_alias_back_compat():
    """The old `AVG_TOKENS_PER_MSG` constant still resolves so external callers
    don't break."""
    assert pipeline_mod.AVG_TOKENS_PER_MSG == 40  # EN baseline
