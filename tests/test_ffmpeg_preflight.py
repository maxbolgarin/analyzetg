"""ffmpeg preflight: friendly exit when the binary is missing.

Doctor checks ffmpeg, but commands that need it should surface the
"install ffmpeg" banner BEFORE doing expensive setup work (Telegram
sync, YouTube metadata fetch). The preflight exits cleanly with a
platform-specific install hint instead of failing mid-pipeline.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import typer

from unread.util.preflight import _ffmpeg_install_hint, require_ffmpeg


def test_install_hint_mentions_brew_on_darwin():
    with patch("unread.util.preflight.platform.system", return_value="Darwin"):
        hint = _ffmpeg_install_hint()
    assert "brew install ffmpeg" in hint


def test_install_hint_covers_linux_managers():
    with patch("unread.util.preflight.platform.system", return_value="Linux"):
        hint = _ffmpeg_install_hint()
    assert "apt install ffmpeg" in hint
    assert "dnf install ffmpeg" in hint
    assert "pacman" in hint


def test_install_hint_covers_windows():
    with patch("unread.util.preflight.platform.system", return_value="Windows"):
        hint = _ffmpeg_install_hint()
    assert "choco" in hint or "scoop" in hint or "gyan.dev" in hint


def test_require_ffmpeg_no_op_when_present():
    # shutil.which returns a truthy path → no exit raised.
    with patch("unread.util.preflight.shutil.which", return_value="/usr/bin/ffmpeg"):
        # Should not raise.
        require_ffmpeg("transcribe voice")


def test_require_ffmpeg_exits_with_install_hint(capsys):
    with patch("unread.util.preflight.shutil.which", return_value=None), pytest.raises(typer.Exit) as ei:
        require_ffmpeg("transcribe voice")
    assert ei.value.exit_code == 1
    out = capsys.readouterr().out
    assert "ffmpeg" in out.lower()
    assert "transcribe voice" in out
    # Per-platform hint is included
    assert "brew" in out or "apt" in out or "choco" in out or "ffmpeg.org" in out
