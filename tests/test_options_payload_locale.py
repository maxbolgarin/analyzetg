"""`AnalysisOptions.options_payload` cache-key invariants.

After the v1.x three-axis split:

* The cache-payload key is **still spelled** `"content_language"` even
  though the underlying field was renamed `content_language` →
  `report_language`. The wire format is sticky on purpose: renaming
  the key would change every existing row's hash and force a global
  re-analysis on upgrade.
* The *value* of that cache key is the resolved **report language** —
  what the LLM writes the analysis in. Toggling the UI `language`
  while `report_language` stays fixed must NOT change the hash.
* The new `locale.content_language` field (Whisper-style source hint)
  is conditionally emitted under a separate `"source_language"` cache
  key — present only when set, so users who never opt in keep the
  same cache rows they had before the field was introduced.
"""

from __future__ import annotations

from unread.analyzer.pipeline import AnalysisOptions
from unread.analyzer.prompts import get_presets
from unread.config import get_settings, reset_settings


def _payload_for(
    lang: str,
    rlang: str | None = None,
    *,
    source_lang: str = "",
) -> dict:
    reset_settings()
    s = get_settings()
    s.locale.language = lang
    s.locale.report_language = rlang or ""
    s.locale.content_language = source_lang
    try:
        # Preset directory is driven by the report language; resolve the
        # same way the live pipeline would so the lookup matches.
        rl = (rlang or lang or "en").lower()
        opts = AnalysisOptions(preset="digest")
        return opts.options_payload(get_presets(rl)["digest"])
    finally:
        reset_settings()


def test_options_payload_contains_report_language_under_legacy_key():
    p = _payload_for("en")
    # The cache key keeps its v0 spelling for back-compat. UI language
    # is not in the key.
    assert "content_language" in p
    assert "language" not in p


def test_options_payload_busts_cache_when_report_language_flips():
    # Same UI language, different report language → different payloads.
    p_en = _payload_for("en", rlang="en")
    p_ru = _payload_for("en", rlang="ru")
    assert p_en != p_ru
    assert p_en["content_language"] == "en"
    assert p_ru["content_language"] == "ru"


def test_options_payload_does_not_bust_on_ui_language_only():
    """Flipping ONLY the UI `locale.language` while `report_language`
    stays fixed must NOT change the cache key — the LLM never sees
    `language`."""
    p_a = _payload_for("en", rlang="ru")
    p_b = _payload_for("ru", rlang="ru")
    assert p_a == p_b


def test_options_payload_report_language_falls_back_to_language():
    p = _payload_for("ru", rlang="")
    # Empty report_language → resolved as `language`.
    assert p["content_language"] == "ru"


def test_options_payload_explicit_report_language_overrides():
    p = _payload_for("ru", rlang="en")
    assert p["content_language"] == "en"


def test_options_payload_unchanged_when_source_language_unset():
    """Default (no source-language hint) — must not add a new key, so
    existing cache rows from before the v1.x split still match."""
    p = _payload_for("ru", rlang="ru")
    assert "source_language" not in p


def test_options_payload_busts_cache_when_source_language_set():
    """Setting the Whisper-style source hint must enter the cache key —
    the system prompt the LLM sees changes."""
    baseline = _payload_for("ru", rlang="ru")
    with_hint = _payload_for("ru", rlang="ru", source_lang="zh")
    assert baseline != with_hint
    assert with_hint.get("source_language") == "zh"
    # Same report language → the legacy `content_language` key value
    # is unchanged; only the new `source_language` key fires the diff.
    assert baseline["content_language"] == with_hint["content_language"]


def test_options_payload_source_language_normalized():
    """Whitespace + case in the hint should be normalized so equivalent
    settings produce equal cache hashes."""
    a = _payload_for("ru", rlang="ru", source_lang=" ZH ")
    b = _payload_for("ru", rlang="ru", source_lang="zh")
    assert a == b
