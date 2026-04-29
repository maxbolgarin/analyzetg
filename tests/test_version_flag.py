"""`unread --version` and `unread -V` print the package version and exit 0."""

from __future__ import annotations

import subprocess
import sys

from unread import __version__


def test_version_flag_long():
    """`unread --version` prints `unread {__version__}` and exits cleanly."""
    result = subprocess.run(
        [sys.executable, "-m", "unread.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"non-zero exit: {result.stderr!r}"
    assert __version__ in result.stdout
    assert "unread" in result.stdout.lower()


def test_version_flag_short():
    """`-V` is the short alias."""
    result = subprocess.run(
        [sys.executable, "-m", "unread.cli", "-V"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout
