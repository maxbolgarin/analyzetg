"""Per-language preset loading + composer dispatch."""

from __future__ import annotations

from unread.analyzer import prompts


def test_get_presets_reads_each_language_directory():
    en = prompts.get_presets("en")
    ru = prompts.get_presets("ru")
    # Both ship the foundation set so existing flows work in either language.
    for name in ("summary", "tldr", "digest", "action_items", "decisions"):
        assert name in en, f"{name} missing in EN preset tree"
        assert name in ru, f"{name} missing in RU preset tree"
    # Same `prompt_version` is fine but bodies differ — pin one obvious diff.
    assert en["summary"].system != ru["summary"].system


def test_hidden_presets_are_loaded_but_marked():
    """The `hidden: true` frontmatter is a wizard-picker filter, not a
    catalog filter — `get_presets` returns hidden presets so the CLI's
    `--preset` flag and routing logic (single-message detection,
    `runner.py` → `multichat`, YouTube/website adapters) can still
    look them up by name. The wizard reads `Preset.hidden` and skips
    them at render time."""
    en = prompts.get_presets("en")
    ru = prompts.get_presets("ru")
    expected_hidden = {"single_msg", "multichat", "video", "website"}
    for tree, label in ((en, "en"), (ru, "ru")):
        # All four routing-targeted presets exist in the catalog.
        for name in expected_hidden:
            assert name in tree, f"{name} missing in {label} preset tree"
            assert tree[name].hidden, f"{label}/{name} should have hidden=true"
        # The user-facing presets stay visible.
        for name in ("summary", "tldr", "digest", "highlights"):
            assert tree[name].hidden is False, f"{label}/{name} must not be hidden"


def test_get_presets_unknown_language_falls_back_to_en():
    """Unknown report_language falls back to presets/en/ with a warning log.
    `compose_system_prompt` injects an explicit OUTPUT LANGUAGE directive,
    so the LLM still writes in the requested language even though the
    preset bodies were authored in English.
    """
    prompts.clear_preset_cache()
    en = prompts.get_presets("en")
    pt = prompts.get_presets("pt")
    assert set(pt.keys()) == set(en.keys())


def test_compose_appends_no_extra_when_language_is_en():
    """EN is the default; `compose_system_prompt(..., language="en")` must
    not append a 'Respond in English' line — the prompts are already English."""
    composed = compose_en = prompts.compose_system_prompt("preset task", topic_titles=None, language="en")
    assert "Respond strictly" not in composed
    assert composed.endswith("preset task")
    # Asserting the variable to keep the linter happy.
    assert compose_en is composed


def test_compose_uses_per_language_base_and_forum():
    en = prompts.compose_system_prompt("X", topic_titles={1: "A"}, language="en")
    ru = prompts.compose_system_prompt("X", topic_titles={1: "A"}, language="ru")
    # Each language pulls its own base + forum addendum from its dir.
    assert "Forum mode" in en
    assert "Форум-режим" in ru
    # Cross-pollination guard: EN composition never contains the RU heading.
    assert "Форум-режим" not in en
    assert "Forum mode" not in ru


def test_compose_uses_language_to_pick_preset_directory():
    """`language` here means the prompt-side / content language. Picking
    "ru" loads `presets/ru/_base.md` so the LLM gets Russian instructions
    even when the UI is in another language."""
    composed_ru = prompts.compose_system_prompt("X", topic_titles=None, language="ru")
    composed_en = prompts.compose_system_prompt("X", topic_titles=None, language="en")
    # The cyrillic phrasing appears only in the RU base.
    assert "потока сообщений" in composed_ru
    assert "потока сообщений" not in composed_en


def test_base_version_is_set():
    """`BASE_VERSION` is part of every preset's `analysis_cache` key — it
    must be a non-empty string. Reset to "v1" at pre-release; bump on any
    structural change to `_base.md` / `_reduce.md` / forum addendum after
    the first public release."""
    assert isinstance(prompts.BASE_VERSION, str)
    assert prompts.BASE_VERSION


def test_clear_preset_cache_forces_reload():
    # Prime the EN cache, then clear and confirm it's repopulated lazily.
    prompts.clear_preset_cache()
    en1 = prompts.get_presets("en")
    en2 = prompts.get_presets("en")
    # Same dict object on second call — caching is in effect.
    assert en1 is en2
    prompts.clear_preset_cache()
    en3 = prompts.get_presets("en")
    assert en3 is not en1  # cache cleared → fresh dict
