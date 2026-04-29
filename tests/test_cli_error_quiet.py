"""Top-level error handler renders a one-liner instead of a Rich traceback.

Multi-frame Rich tracebacks panic non-technical users and are rarely
actionable. ``cli._run`` now catches generic exceptions and prints a
friendly "Error: …" line plus a hint to use ``-v``. With the env flag
set, the original exception propagates so power users / bug reports
get the full thing.
"""

from __future__ import annotations

import pytest
import typer

from unread.cli import _run


async def _boom() -> None:
    raise RuntimeError("the spice must flow")


def test_unhandled_exception_renders_one_liner(capsys, monkeypatch):
    # Ensure verbose is OFF
    monkeypatch.delenv("UNREAD_VERBOSE", raising=False)
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
    # The hint mentions -v and bug-report so the user knows how to recover.
    assert "-v" in out
    assert "bug-report" in out


def test_verbose_re_raises_original_exception(monkeypatch):
    monkeypatch.setenv("UNREAD_VERBOSE", "1")
    with pytest.raises(RuntimeError, match="spice"):
        _run(_boom())


async def _exit_clean() -> None:
    raise typer.Exit(2)


def test_typer_exit_passes_through(monkeypatch):
    monkeypatch.delenv("UNREAD_VERBOSE", raising=False)
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
