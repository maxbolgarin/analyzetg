"""Project-level regression tests for release config and CLI help."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from analyzetg.cli import app

ROOT = Path(__file__).resolve().parents[1]


def test_release_config_targets_main_and_installs_used_plugins() -> None:
    release_cfg = json.loads((ROOT / ".releaserc.json").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert release_cfg["branches"] == ["main"]
    assert "@semantic-release/commit-analyzer" in workflow
    assert "@semantic-release/release-notes-generator" in workflow
    assert "@semantic-release/npm" not in workflow


def test_top_level_help_smoke() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "analyze" in result.output
    assert "dump" in result.output
    assert "cache" in result.output


def test_core_command_help_smoke() -> None:
    runner = CliRunner()
    for args in (["analyze", "--help"], ["dump", "--help"], ["cache", "purge", "--help"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
