"""`AnalysisOptions.options_payload` cache-key invariants.

After the v5 semantics flip, only `content_language` enters the cache
key — `locale.language` is UI-only (saved-report headings, wizard) and
does NOT affect any LLM input, so toggling it must NOT bust the cache
(see CLAUDE.md invariant #10 + the comment in `options_payload`).
"""

from __future__ import annotations

from analyzetg.analyzer.pipeline import AnalysisOptions
from analyzetg.analyzer.prompts import get_presets
from analyzetg.config import get_settings, reset_settings


def _payload_for(lang: str, clang: str | None = None) -> dict:
    reset_settings()
    s = get_settings()
    s.locale.language = lang
    s.locale.content_language = clang or ""
    try:
        # Preset directory is now driven by content_language; resolve it
        # the same way the live pipeline would so the lookup matches.
        cl = (clang or lang or "en").lower()
        opts = AnalysisOptions(preset="digest")
        return opts.options_payload(get_presets(cl)["digest"])
    finally:
        reset_settings()


def test_options_payload_contains_content_language():
    p = _payload_for("en")
    # Only content_language is in the cache key now — UI language is not.
    assert "content_language" in p
    assert "language" not in p


def test_options_payload_busts_cache_when_content_language_flips():
    # Same UI language, different content_language → different payloads.
    p_en = _payload_for("en", clang="en")
    p_ru = _payload_for("en", clang="ru")
    assert p_en != p_ru
    assert p_en["content_language"] == "en"
    assert p_ru["content_language"] == "ru"


def test_options_payload_does_not_bust_on_ui_language_only():
    """Flipping ONLY the UI `locale.language` while content_language stays
    fixed must NOT change the cache key — the LLM never sees `language`."""
    p_a = _payload_for("en", clang="ru")
    p_b = _payload_for("ru", clang="ru")
    assert p_a == p_b


def test_options_payload_content_language_falls_back_to_language():
    p = _payload_for("ru", clang="")
    # Empty content_language → resolved as `language`.
    assert p["content_language"] == "ru"


def test_options_payload_explicit_content_language_overrides():
    p = _payload_for("ru", clang="en")
    assert p["content_language"] == "en"
