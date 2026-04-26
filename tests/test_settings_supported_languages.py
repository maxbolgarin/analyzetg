"""`_supported_locale_languages()` only lists languages the service can
actually serve — both a preset tree AND i18n translations. Adding a
language to one half without the other is a usability bug (silent EN
fallback or "directory not found"); the picker hides such half-baked
languages.
"""

from __future__ import annotations

from analyzetg.settings.commands import (
    _supported_audio_languages,
    _supported_locale_languages,
)


def test_supported_locale_languages_lists_en_and_ru_today():
    """Both bundled languages must show up. If one disappears, either a
    preset file got dropped or its i18n entries did — both are bugs."""
    supported = _supported_locale_languages()
    assert "en" in supported
    assert "ru" in supported


def test_supported_locale_languages_puts_english_first():
    supported = _supported_locale_languages()
    assert supported[0] == "en"


def test_supported_locale_languages_skips_languages_without_presets(tmp_path, monkeypatch):
    """A language with i18n entries but no `presets/<code>/` tree must NOT
    appear in the picker — `compose_system_prompt` would explode."""
    from analyzetg import i18n as i18n_mod
    from analyzetg.analyzer import prompts as prompts_mod

    # Pretend "xx" has an i18n entry but no preset tree.
    original = dict(i18n_mod._STRINGS)
    i18n_mod._STRINGS["period_label"] = {**original["period_label"], "xx": "TEST"}
    try:
        # Point preset dir at a tmp tree with only en/ and ru/ (no xx/).
        (tmp_path / "en").mkdir()
        (tmp_path / "en" / "_base.md").write_text("base")
        (tmp_path / "en" / "_reduce.md").write_text("reduce")
        (tmp_path / "ru").mkdir()
        (tmp_path / "ru" / "_base.md").write_text("base")
        (tmp_path / "ru" / "_reduce.md").write_text("reduce")
        monkeypatch.setattr(prompts_mod, "PRESETS_DIR", tmp_path)
        supported = _supported_locale_languages()
        assert "xx" not in supported
        assert supported == ["en", "ru"]
    finally:
        i18n_mod._STRINGS["period_label"] = original["period_label"]


def test_supported_locale_languages_skips_languages_without_i18n(tmp_path, monkeypatch):
    """Inverse: a directory without i18n entries doesn't qualify either —
    the picker would offer a language whose UI labels are silently English."""
    from analyzetg.analyzer import prompts as prompts_mod

    (tmp_path / "en").mkdir()
    (tmp_path / "en" / "_base.md").write_text("base")
    (tmp_path / "en" / "_reduce.md").write_text("reduce")
    (tmp_path / "ru").mkdir()
    (tmp_path / "ru" / "_base.md").write_text("base")
    (tmp_path / "ru" / "_reduce.md").write_text("reduce")
    # `de/` has the files but no i18n strings → shouldn't be listed.
    (tmp_path / "de").mkdir()
    (tmp_path / "de" / "_base.md").write_text("base")
    (tmp_path / "de" / "_reduce.md").write_text("reduce")
    monkeypatch.setattr(prompts_mod, "PRESETS_DIR", tmp_path)
    supported = _supported_locale_languages()
    assert "de" not in supported
    assert "en" in supported and "ru" in supported


def test_supported_audio_languages_starts_with_ui_supported():
    """Audio picker is broader (Whisper accepts more codes) but its
    head must match the UI-supported set so RU/EN users see their
    natural choice on top."""
    audio = _supported_audio_languages()
    ui = _supported_locale_languages()
    assert audio[: len(ui)] == ui
    # Common spoken languages added after the UI-supported anchors.
    assert "de" in audio
    assert "ja" in audio
    assert len(audio) > len(ui)
