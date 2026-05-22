"""Log-mode resolution: CLI flag > env var > config > default.

The mode controls three things at once: structlog level, whether the
arrow-status `console.print` lines render, and whether Rich tracebacks
expose locals on unhandled exceptions. Modes:

* ``silent`` — ERROR level, no status arrows, no progress bars
* ``normal`` — WARNING level, status arrows + progress (default)
* ``verbose`` — INFO level, everything except DEBUG and Rich locals
* ``debug``   — DEBUG level + Rich tracebacks (the previous ``-v``)
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from unread.config import LoggingCfg, reset_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test starts with a clean env for UNREAD_LOG_MODE / UNREAD_DEBUG."""
    monkeypatch.delenv("UNREAD_LOG_MODE", raising=False)
    monkeypatch.delenv("UNREAD_DEBUG", raising=False)
    monkeypatch.delenv("UNREAD_VERBOSE", raising=False)
    reset_settings()
    yield
    reset_settings()


# ----- LoggingCfg field ------------------------------------------------


def test_logging_mode_defaults_to_normal():
    """Fresh install with no config / env → normal mode."""
    cfg = LoggingCfg()
    assert cfg.mode == "normal"


def test_logging_mode_accepts_all_four_values():
    for value in ("silent", "normal", "verbose", "debug"):
        cfg = LoggingCfg(mode=value)
        assert cfg.mode == value


def test_logging_mode_rejects_unknown_value():
    """Strict-cfg: typos in config.toml must raise, not silently fall back."""
    with pytest.raises(ValidationError):
        LoggingCfg(mode="quiet")  # close-but-wrong


# ----- resolve_log_mode precedence ------------------------------------


def test_resolve_log_mode_default_normal():
    from unread.util.logging import resolve_log_mode

    assert resolve_log_mode(cli_flag=None, settings_mode="normal") == "normal"


def test_resolve_log_mode_settings_overrides_default():
    from unread.util.logging import resolve_log_mode

    assert resolve_log_mode(cli_flag=None, settings_mode="silent") == "silent"


def test_resolve_log_mode_env_overrides_settings(monkeypatch):
    from unread.util.logging import resolve_log_mode

    monkeypatch.setenv("UNREAD_LOG_MODE", "verbose")
    assert resolve_log_mode(cli_flag=None, settings_mode="silent") == "verbose"


def test_resolve_log_mode_cli_overrides_env(monkeypatch):
    from unread.util.logging import resolve_log_mode

    monkeypatch.setenv("UNREAD_LOG_MODE", "verbose")
    assert resolve_log_mode(cli_flag="silent", settings_mode="normal") == "silent"


def test_resolve_log_mode_ignores_invalid_env(monkeypatch):
    """Garbage in env → fall through to settings. Lenient because env vars
    come from arbitrary shells; raising would block every command."""
    from unread.util.logging import resolve_log_mode

    monkeypatch.setenv("UNREAD_LOG_MODE", "quiet")  # invalid
    assert resolve_log_mode(cli_flag=None, settings_mode="silent") == "silent"


def test_resolve_log_mode_rejects_invalid_cli_flag():
    """CLI flag is set by us, not the user — a bad value is a bug, not a typo."""
    from unread.util.logging import resolve_log_mode

    with pytest.raises(ValueError):
        resolve_log_mode(cli_flag="bogus", settings_mode="normal")


# ----- mode → structlog level ----------------------------------------


@pytest.mark.parametrize(
    "mode,expected_level",
    [
        ("silent", logging.ERROR),
        ("normal", logging.WARNING),
        ("verbose", logging.INFO),
        ("debug", logging.DEBUG),
    ],
)
def test_setup_logging_picks_level_for_mode(mode: str, expected_level: int):
    from unread.util.logging import setup_logging

    setup_logging(mode=mode)
    assert logging.getLogger().level == expected_level


def test_setup_logging_default_is_normal():
    from unread.util.logging import setup_logging

    setup_logging()
    assert logging.getLogger().level == logging.WARNING


# ----- Rich-traceback gate (security) ---------------------------------


def test_rich_tracebacks_only_in_debug_mode(monkeypatch):
    """Rich tracebacks render local-variable values on unhandled exceptions
    — which can include API keys. Must be gated to debug mode ONLY, not
    promoted to `verbose` by accident."""
    import os

    from unread.util.logging import setup_logging

    monkeypatch.delenv("UNREAD_DEBUG", raising=False)

    setup_logging(mode="silent")
    assert os.environ.get("UNREAD_DEBUG") != "1"
    setup_logging(mode="normal")
    assert os.environ.get("UNREAD_DEBUG") != "1"
    setup_logging(mode="verbose")
    # `verbose` is INFO-level — it must NOT set the debug-tracebacks env var.
    assert os.environ.get("UNREAD_DEBUG") != "1"
    setup_logging(mode="debug")
    assert os.environ.get("UNREAD_DEBUG") == "1"


# ----- status_print helper --------------------------------------------


def test_status_print_suppressed_in_silent_mode(capsys):
    """Silent mode hides arrow status lines (`→ Resolving …`) but keeps
    errors and the final report flowing through `console.print` directly.
    """
    from unread.util.logging import set_log_mode, status_print

    set_log_mode("silent")
    status_print("→ should not appear")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "should not appear" not in out


def test_status_print_emits_in_normal_mode(capsys):
    from unread.util.logging import set_log_mode, status_print

    set_log_mode("normal")
    status_print("→ should appear")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "should appear" in combined


def test_status_print_emits_in_verbose_mode(capsys):
    from unread.util.logging import set_log_mode, status_print

    set_log_mode("verbose")
    status_print("→ should appear")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "should appear" in combined


def test_is_silent_helper():
    from unread.util.logging import is_silent, set_log_mode

    set_log_mode("silent")
    assert is_silent() is True
    set_log_mode("normal")
    assert is_silent() is False
    set_log_mode("verbose")
    assert is_silent() is False
    set_log_mode("debug")
    assert is_silent() is False


# ----- CLI flag conflict ----------------------------------------------


def test_cli_quiet_and_verbose_conflict_raises():
    """`-q` + `-v` together is ambiguous and almost certainly a typo —
    reject with a clear message rather than picking one silently."""
    from unread.util.logging import resolve_cli_log_mode

    with pytest.raises(ValueError, match="cannot combine"):
        resolve_cli_log_mode(quiet=True, verbose=True, debug=False)


def test_cli_quiet_and_debug_conflict_raises():
    from unread.util.logging import resolve_cli_log_mode

    with pytest.raises(ValueError, match="cannot combine"):
        resolve_cli_log_mode(quiet=True, verbose=False, debug=True)


def test_cli_flags_resolve_to_mode():
    from unread.util.logging import resolve_cli_log_mode

    assert resolve_cli_log_mode(quiet=False, verbose=False, debug=False) is None
    assert resolve_cli_log_mode(quiet=True, verbose=False, debug=False) == "silent"
    assert resolve_cli_log_mode(quiet=False, verbose=True, debug=False) == "verbose"
    assert resolve_cli_log_mode(quiet=False, verbose=False, debug=True) == "debug"


def test_cli_verbose_and_debug_picks_debug():
    """`-v --debug` together is harmless — both mean 'more output', and
    debug is the stricter superset. Pick debug rather than rejecting."""
    from unread.util.logging import resolve_cli_log_mode

    assert resolve_cli_log_mode(quiet=False, verbose=True, debug=True) == "debug"


# ----- _OVERRIDE_KEYS allowlist --------------------------------------


def test_logging_mode_is_an_override_key():
    """`unread settings set logging.mode silent` must work — that requires
    the key on the allowlist."""
    from unread.db._keys import OVERRIDE_KEYS

    assert "logging.mode" in OVERRIDE_KEYS


def test_apply_one_override_sets_logging_mode():
    """The bootstrap overlay must wire `logging.mode` onto the settings
    singleton, otherwise the persisted value is silently ignored."""
    from unread.config import get_settings
    from unread.db.repo import _apply_one_override

    s = get_settings()
    _apply_one_override(s, "logging.mode", "silent")
    assert s.logging.mode == "silent"


def test_apply_one_override_ignores_invalid_logging_mode():
    """Garbage in the DB doesn't crash the bootstrap — invalid value is
    silently ignored and the config default stays in effect."""
    from unread.config import get_settings
    from unread.db.repo import _apply_one_override

    s = get_settings()
    s.logging.mode = "normal"
    _apply_one_override(s, "logging.mode", "loud")
    assert s.logging.mode == "normal"


# ----- pipeline-level _status helper integration ---------------------


def test_core_pipeline_status_suppressed_in_silent(capsys):
    """The pipeline's `_status(...)` (used for `→ Resolving …` style arrow
    lines) must no-op in silent mode. Errors (`[red]`) and warnings
    (`[yellow]`) still go through `console.print` and stay visible."""
    from unread.core import pipeline as p
    from unread.util.logging import set_log_mode

    set_log_mode("silent")
    p._status("[grey70]→ progress chatter[/]")
    p.console.print("[red]boom[/]")
    p.console.print("[yellow]heads up[/]")

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "progress chatter" not in combined
    assert "boom" in combined
    assert "heads up" in combined


def test_core_pipeline_status_emits_in_normal(capsys):
    from unread.core import pipeline as p
    from unread.util.logging import set_log_mode

    set_log_mode("normal")
    p._status("[grey70]→ progress chatter[/]")
    combined = capsys.readouterr().out + capsys.readouterr().err
    assert "progress chatter" in combined


# ----- `unread settings` registry integration ------------------------


def test_settings_registry_includes_log_mode_row():
    """`unread settings` (interactive editor) must surface `logging.mode`
    so users can pick silent/normal/verbose/debug without editing
    config.toml by hand."""
    from unread.settings.commands import _BY_KEY, _SETTINGS

    sd = _BY_KEY.get("logging.mode")
    assert sd is not None, "logging.mode missing from _SETTINGS"
    assert sd.kind == "log_mode"
    assert sd.category_key == "settings_cat_output"
    # i18n keys exist (raises KeyError on lookup if they don't, so just
    # touching the properties is enough).
    assert sd.label
    assert sd.desc
    # Reachable via the combined registry too.
    assert sd in _SETTINGS


def test_log_mode_current_display_reads_settings():
    """The picker row's right-hand-side current-value column reads
    `s.logging.mode`, not the override dict directly (so the displayed
    value matches what's actually active)."""
    from unread.config import get_settings
    from unread.settings.commands import _BY_KEY, _current_display

    sd = _BY_KEY["logging.mode"]
    s = get_settings()
    s.logging.mode = "verbose"
    assert _current_display(sd, overrides={}, s=s) == "verbose"
    s.logging.mode = "silent"
    assert _current_display(sd, overrides={}, s=s) == "silent"


def test_log_mode_appears_under_output_category():
    """It belongs in its own 'Output verbosity' category — both for menu
    grouping AND so future output-related rows (e.g. progress style)
    have a natural home."""
    from unread.i18n import t as _t
    from unread.settings.commands import _BY_KEY

    sd = _BY_KEY["logging.mode"]
    # i18n category label exists (lookup raises if missing).
    assert _t("settings_cat_output")
    assert sd.category == _t("settings_cat_output")
