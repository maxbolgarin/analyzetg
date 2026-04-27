"""Preset frontmatter parses the new `enrich:` list."""

from __future__ import annotations

from pathlib import Path

from unread.analyzer.prompts import _load_preset_file


def test_preset_parses_enrich_list(tmp_path: Path):
    p = tmp_path / "demo.md"
    p.write_text(
        "---\n"
        "name: demo\n"
        "prompt_version: v1\n"
        "enrich: [link, image]\n"
        "---\n"
        "system prompt body here\n"
        "---USER---\n"
        "user template {period} {title} {msg_count} {messages}\n",
        encoding="utf-8",
    )
    preset = _load_preset_file(p)
    assert preset.enrich_kinds == ["link", "image"]


def test_preset_empty_enrich_default(tmp_path: Path):
    p = tmp_path / "demo.md"
    p.write_text(
        "---\nname: demo\nprompt_version: v1\n---\nbody\n",
        encoding="utf-8",
    )
    preset = _load_preset_file(p)
    assert preset.enrich_kinds == []


def test_preset_enrich_bare_csv(tmp_path: Path):
    p = tmp_path / "demo.md"
    p.write_text(
        "---\nname: demo\nprompt_version: v1\nenrich: voice, videonote\n---\nbody\n",
        encoding="utf-8",
    )
    preset = _load_preset_file(p)
    assert preset.enrich_kinds == ["voice", "videonote"]
