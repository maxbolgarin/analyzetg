"""Preset loading: frontmatter parsing, system/user split, custom presets.

Covers regressions in:
- `output_budget_tokens` / `map_output_tokens` parsing (both are used by
  the pipeline to cap LLM responses; wrong parse → truncation).
- `---USER---` marker splitting system prompt from user template.
- Custom-preset loader (`atg analyze --preset custom --prompt-file ...`).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from atg.analyzer.prompts import (
    DEFAULT_USER_TAIL,
    PRESETS,
    PRESETS_DIR,
    USER_MARKER,
    Preset,
    _parse_frontmatter,
    load_custom_preset,
)


def test_parse_frontmatter_basic() -> None:
    text = "---\nname: foo\nversion: v1\n---\nhello body\n"
    meta, body = _parse_frontmatter(text)
    assert meta == {"name": "foo", "version": "v1"}
    assert body.strip() == "hello body"


def test_parse_frontmatter_missing_returns_empty_meta() -> None:
    # No frontmatter → meta empty, full text is body.
    meta, body = _parse_frontmatter("just a body, no frontmatter")
    assert meta == {}
    assert body == "just a body, no frontmatter"


def test_all_presets_render_with_standard_kwargs() -> None:
    """Regression: the pipeline calls `preset.render_user(period, title,
    msg_count, messages)` for every preset. A preset that accidentally has a
    stray `{var}` in its user template crashes run_analysis with KeyError.
    """
    for name, preset in PRESETS.items():
        rendered = preset.render_user(
            period="test-period",
            title="test-title",
            msg_count=1,
            messages="test messages body",
        )
        assert "test messages body" in rendered, f"preset {name!r} dropped {{messages}}"


def test_parse_frontmatter_skips_comment_lines() -> None:
    text = "---\n# this is a comment\nname: foo\n---\nbody"
    meta, _ = _parse_frontmatter(text)
    assert meta == {"name": "foo"}


def test_all_builtin_presets_load() -> None:
    # Every preset in presets/ must load with the required fields set.
    assert "summary" in PRESETS
    for name, p in PRESETS.items():
        assert p.name == name, f"preset {name!r} has wrong name field"
        assert p.system, f"preset {name!r} has empty system prompt"
        assert p.user_template, f"preset {name!r} has empty user template"
        assert p.output_budget_tokens > 0
        assert p.map_output_tokens > 0
        # Pipeline expects these four placeholders in the user template.
        for key in ("{period}", "{title}", "{msg_count}", "{messages}"):
            assert key in p.user_template, f"preset {name!r} missing placeholder {key}"


def test_builtin_presets_are_included_in_wheel() -> None:
    # Non-editable installs do not have the repository checkout next to the
    # package, so the wheel must carry the builtin preset markdown tree.
    # Per-language directories (presets/<lang>/) — both must ship.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    force_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include.get("presets") == "presets"
    assert (PRESETS_DIR / "ru" / "summary.md").is_file()
    assert (PRESETS_DIR / "en" / "summary.md").is_file()


def test_summary_preset_has_adequate_budget() -> None:
    # The distilled summary is intentionally tighter than the old recap-style
    # one — but it still needs room for Главное + Идеи/решения + Стоит
    # посмотреть sections. Dropping below ~2000 risks re-introducing the
    # truncation bug that originally drove budgets up.
    p = PRESETS["summary"]
    assert p.output_budget_tokens >= 2000, (
        f"summary output_budget_tokens={p.output_budget_tokens} is cutting it close — "
        "truncation will silently return partial results."
    )


def test_broad_preset_preserves_original_summary_scope() -> None:
    # The old `summary` moved to `broad` — it's the structured recap with
    # Top-3 themes + bullet points + tone + key messages and wants a fatter
    # budget. Tests pin it so a future tidy-up doesn't quietly shrink it.
    p = PRESETS["broad"]
    assert p.output_budget_tokens >= 4000
    assert p.map_output_tokens >= 2000


def test_custom_preset_from_file(tmp_path: Path) -> None:
    p = tmp_path / "my.md"
    p.write_text(
        "---\n"
        "name: my-preset\n"
        "prompt_version: v9\n"
        "output_budget_tokens: 2500\n"
        "map_output_tokens: 800\n"
        "---\n"
        "You are my custom analyst.\n"
        f"{USER_MARKER}\n"
        "Period: {period}\nChat: {title}\nCount: {msg_count}\n{messages}\n",
        encoding="utf-8",
    )
    preset = load_custom_preset(p)
    assert isinstance(preset, Preset)
    assert preset.name == "my-preset"
    assert preset.prompt_version == "v9"
    assert preset.output_budget_tokens == 2500
    assert preset.map_output_tokens == 800
    assert "custom analyst" in preset.system
    assert "{messages}" in preset.user_template
    # Bodies aren't conflated by the marker:
    assert USER_MARKER not in preset.system
    assert USER_MARKER not in preset.user_template


def test_custom_preset_without_user_marker_uses_default_tail(tmp_path: Path) -> None:
    p = tmp_path / "noreduce.md"
    p.write_text(
        "---\nname: sys-only\nprompt_version: v1\n---\nJust a system prompt, no user template.\n",
        encoding="utf-8",
    )
    preset = load_custom_preset(p)
    assert "Just a system prompt" in preset.system
    # Default tail is appended so pipeline placeholders still render.
    # `DEFAULT_USER_TAIL` is now per-language; assert the EN default's first
    # line shows up (load_custom_preset defaults to language="en").
    assert "{messages}" in preset.user_template
    assert DEFAULT_USER_TAIL["en"].split("\n")[0] in preset.user_template


def test_custom_preset_injects_missing_placeholders(tmp_path: Path) -> None:
    # A user template missing any required placeholder gets them appended,
    # so .format() never blows up with KeyError at render time.
    p = tmp_path / "partial.md"
    p.write_text(
        "---\nname: partial\n---\n"
        "Sys prompt.\n"
        f"{USER_MARKER}\n"
        "Only period here: {period}\n",  # missing {title}, {msg_count}, {messages}
        encoding="utf-8",
    )
    preset = load_custom_preset(p)
    # render_user must not raise — all four keys should resolve.
    rendered = preset.render_user(period="P", title="T", msg_count=1, messages="M")
    assert "P" in rendered and "T" in rendered and "M" in rendered


def test_preset_render_user_raises_on_extra_braces(tmp_path: Path) -> None:
    # Edge case: template with a literal curly-brace token that isn't a
    # placeholder should either render (it's legal .format syntax: {{ }}) or
    # raise clearly; either way, we don't want silent garbage.
    p = tmp_path / "curly.md"
    p.write_text(
        f"---\nname: curly\n---\nSys.\n{USER_MARKER}\n{{period}} {{title}} {{msg_count}} {{messages}}\n",
        encoding="utf-8",
    )
    preset = load_custom_preset(p)
    out = preset.render_user(period="X", title="Y", msg_count=3, messages="Z")
    assert "X Y 3 Z" in out


def test_custom_preset_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_custom_preset(tmp_path / "does_not_exist.md")
