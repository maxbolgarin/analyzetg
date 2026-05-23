"""unread — terminal banner.

Drop-in, zero-dependency. Prints the brand mark on startup / --help / --version.
Respects the NO_COLOR convention (https://no-color.org) and disables color
when stdout is not a TTY (pipes, CI logs), so it never leaks escape codes.

Usage:
    from unread.banner import print_banner
    print_banner(version="0.1.0")
"""

from __future__ import annotations

import os
import sys

# Single source of truth for the accent. Swap to (34, 197, 94) for green.
ACCENT = (59, 130, 246)  # #3b82f6

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def banner(version: str = "", *, color: bool | None = None, stream=None) -> str:
    """Return the banner as a string (with or without ANSI color)."""
    stream = stream or sys.stdout
    if color is None:
        color = _supports_color(stream)

    ver = f" {version}" if version else ""
    tagline = "read your unread \u2014 without reading it."

    if not color:
        return f"[*] unread{ver}\n    {tagline}"

    r, g, b = ACCENT
    dot = f"\x1b[38;2;{r};{g};{b}m\u25cf{RESET}"
    head = f"{BOLD}[{dot}{BOLD}] unread{RESET}{DIM}{ver}{RESET}"
    sub = f"{DIM}    {tagline}{RESET}"
    return f"{head}\n{sub}"


def print_banner(version: str = "", *, stream=None) -> None:
    stream = stream or sys.stdout
    print(banner(version, stream=stream), file=stream)


if __name__ == "__main__":
    print_banner(version="0.1.0")
