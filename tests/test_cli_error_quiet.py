"""Top-level error handler renders a one-liner instead of a Rich traceback.

Multi-frame Rich tracebacks panic non-technical users and are rarely
actionable. ``cli._run`` catches generic exceptions and prints a
friendly "Error: …" line plus a hint to use ``--debug``. With
``UNREAD_DEBUG=1`` (set by `--debug` flag or by the user manually), the
original exception propagates so power users / bug reports get the
full thing. ``-v / --verbose`` no longer enables tracebacks (it now
means INFO-level structured logs only — Rich tracebacks expose locals
that may include API keys, so they're gated to `--debug`).
"""

from __future__ import annotations

import pytest
import typer

from unread.cli import _run


async def _boom() -> None:
    raise RuntimeError("the spice must flow")


def test_unhandled_exception_renders_one_liner(capsys, monkeypatch):
    monkeypatch.delenv("UNREAD_DEBUG", raising=False)

    with pytest.raises(typer.Exit) as ei:
        _run(_boom())
    assert ei.value.exit_code == 1

    out = capsys.readouterr().out
    assert "Error:" in out
    assert "the spice must flow" in out
    # No Python traceback / file-path frames should leak.
    assert "Traceback" not in out
    assert "asyncio" not in out
    # The hint mentions --debug and bug-report so the user knows how to recover.
    assert "--debug" in out
    assert "bug-report" in out


def test_debug_re_raises_original_exception(monkeypatch):
    """`UNREAD_DEBUG=1` (set by the `--debug` flag, or by hand) opts back
    in to the full Rich traceback."""
    monkeypatch.setenv("UNREAD_DEBUG", "1")
    with pytest.raises(RuntimeError, match="spice"):
        _run(_boom())


def test_verbose_env_no_longer_triggers_traceback(monkeypatch):
    """Old `UNREAD_VERBOSE=1` is retired — only `UNREAD_DEBUG=1` re-raises
    now. `verbose` is INFO-level logs without the security-sensitive
    locals-leaking traceback."""
    monkeypatch.setenv("UNREAD_VERBOSE", "1")
    monkeypatch.delenv("UNREAD_DEBUG", raising=False)
    with pytest.raises(typer.Exit) as ei:
        _run(_boom())
    assert ei.value.exit_code == 1


async def _exit_clean() -> None:
    raise typer.Exit(2)


def test_typer_exit_passes_through(monkeypatch):
    monkeypatch.delenv("UNREAD_DEBUG", raising=False)
    with pytest.raises(typer.Exit) as ei:
        _run(_exit_clean())
    # typer.Exit's exit_code preserved
    assert ei.value.exit_code == 2


async def _interrupted() -> None:
    raise KeyboardInterrupt()


def test_keyboard_interrupt_friendly(capsys):
    with pytest.raises(typer.Exit) as ei:
        _run(_interrupted())
    assert ei.value.exit_code == 130
    out = capsys.readouterr().out
    assert "Cancelled" in out
