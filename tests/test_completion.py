"""Tests for `unread/completion/` — shell completion installer.

Pre-prod gap: the completion package shipped without unit tests.
These pin the shell-detection logic and the per-shell script content
so a refactor of the patched-script blocks can't silently break tab
completion in zsh / fish.
"""

from __future__ import annotations

import pytest

from unread.completion.commands import (
    _PATCHED_SCRIPTS,
    _VALID_SHELLS,
    _completion_script,
    _resolve_shell,
)


def test_valid_shells_contains_the_three_we_support():
    assert set(_VALID_SHELLS) == {"bash", "zsh", "fish"}


def test_patched_scripts_cover_zsh_and_fish_only():
    """bash falls through to typer.completion; only zsh + fish use our
    patched templates."""
    assert set(_PATCHED_SCRIPTS) == {"zsh", "fish"}


@pytest.mark.parametrize("shell", ["zsh", "fish"])
def test_completion_script_returns_patched_template_for_zsh_fish(shell: str):
    """The patched scripts must reference our prog name and use the
    Click-8 envelope (`_UNREAD_COMPLETE`)."""
    script = _completion_script(shell)
    # Sanity: non-empty and references our binary name + the Click env var
    assert "unread" in script
    assert "_UNREAD_COMPLETE" in script
    # Patched scripts are the verbatim module constant
    assert script == _PATCHED_SCRIPTS[shell]


def test_completion_script_for_bash_delegates_to_typer():
    """bash uses Typer's get_completion_script — content can change with
    Typer upgrades, but it must still mention our prog name."""
    script = _completion_script("bash")
    assert "unread" in script
    # Typer's bash script always uses COMP_WORDS / COMP_CWORD from the
    # bash completion spec; pin one stable token.
    assert "COMP_" in script or "complete" in script.lower()


def test_resolve_shell_accepts_explicit_valid_shell():
    """When the user passes --shell zsh, return it lowercased."""
    assert _resolve_shell("zsh") == "zsh"
    assert _resolve_shell("BASH") == "bash"  # case-insensitive
    assert _resolve_shell("  fish  ") == "fish"  # trims whitespace


def test_resolve_shell_rejects_unknown_explicit_shell():
    """`--shell tcsh` exits 1 with a friendly error."""
    import typer

    with pytest.raises(typer.Exit):
        _resolve_shell("tcsh")


def test_resolve_shell_falls_back_to_shell_env_var(monkeypatch):
    """When shellingham fails, $SHELL drives detection."""

    # Force shellingham to fail.
    def _fail():
        import shellingham

        raise shellingham.ShellDetectionFailure()

    monkeypatch.setattr("shellingham.detect_shell", _fail)
    monkeypatch.setenv("SHELL", "/usr/local/bin/zsh")
    assert _resolve_shell(None) == "zsh"


def test_resolve_shell_uses_shellingham_when_available(monkeypatch):
    """Primary: parent-process inspection via shellingham."""
    monkeypatch.setattr("shellingham.detect_shell", lambda: ("fish", "/opt/fish"))
    monkeypatch.setenv("SHELL", "/bin/bash")  # ignored — shellingham wins
    assert _resolve_shell(None) == "fish"


def test_resolve_shell_exits_when_no_detection_works(monkeypatch):
    """Both shellingham and $SHELL absent → friendly exit."""
    import typer

    def _fail():
        import shellingham

        raise shellingham.ShellDetectionFailure()

    monkeypatch.setattr("shellingham.detect_shell", _fail)
    monkeypatch.delenv("SHELL", raising=False)
    with pytest.raises(typer.Exit):
        _resolve_shell(None)


def test_resolve_shell_rejects_detected_unsupported(monkeypatch):
    """Shellingham returns "tcsh" (which Typer doesn't support) → exit."""
    import typer

    monkeypatch.setattr("shellingham.detect_shell", lambda: ("tcsh", "/bin/tcsh"))
    monkeypatch.delenv("SHELL", raising=False)
    with pytest.raises(typer.Exit):
        _resolve_shell(None)
