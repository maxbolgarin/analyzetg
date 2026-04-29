"""Preflight checks for external dependencies.

Catch missing-tool failures at the top of a long-running command rather
than mid-pipeline after the user has already waited for downloads /
syncs. Each check exits with platform-specific install hints so a
non-technical user has a copy-pasteable next step.
"""

from __future__ import annotations

import platform
import shutil

import typer

from unread.util.logging import console


def _ffmpeg_install_hint() -> str:
    """Per-platform install snippet for ffmpeg."""
    system = platform.system().lower()
    if system == "darwin":
        return "  brew install ffmpeg"
    if system == "linux":
        # Cover the most common package managers without being exhaustive
        # — the user can always check their distro docs.
        return (
            "  Debian / Ubuntu:  sudo apt install ffmpeg\n"
            "  Fedora:           sudo dnf install ffmpeg\n"
            "  Arch:             sudo pacman -S ffmpeg\n"
            "  Alpine:           sudo apk add ffmpeg"
        )
    if system == "windows":
        return (
            "  Chocolatey:  choco install ffmpeg\n"
            "  Scoop:       scoop install ffmpeg\n"
            "  Or download from https://www.gyan.dev/ffmpeg/builds/"
        )
    return "  See https://ffmpeg.org/download.html"


def require_ffmpeg(reason: str) -> None:
    """Exit with friendly install instructions when ffmpeg is missing.

    Looks up ``settings.media.ffmpeg_path`` (default ``"ffmpeg"``) on
    PATH; if not found, raises ``typer.Exit(1)`` after printing a
    one-line "what we need" + per-platform install snippet.

    ``reason`` is a short user-facing fragment ("transcribe voice",
    "download YouTube audio") so the user knows which feature triggered
    the requirement.
    """
    from unread.config import get_settings

    s = get_settings()
    configured = s.media.ffmpeg_path or "ffmpeg"
    if shutil.which(configured) or shutil.which("ffmpeg"):
        return

    console.print(
        f"[red]ffmpeg is required to {reason}, but it's not on PATH.[/]\n"
        f"\n"
        f"Install it:\n"
        f"{_ffmpeg_install_hint()}\n"
        f"\n"
        f"After installing, re-run your command. If ffmpeg is at a non-default "
        f"location, set [bold][media][/] [cyan]ffmpeg_path[/] in "
        f"[bold]~/.unread/config.toml[/]."
    )
    raise typer.Exit(1)
