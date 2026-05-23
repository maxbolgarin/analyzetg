"""Terminal brand banner. NO_COLOR-aware, never leaks escape codes when piped.

Brand assets live at `assets/terminal/banner.py` (single source of truth);
this module is a vendored copy so the CLI can import it without depending
on the `assets/` directory at runtime.
"""

from __future__ import annotations

import os
import sys

# Single source of truth for the accent. Matches assets/BRAND.md.
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
    tagline = "read your unread — without reading it."

    if not color:
        return f"[*] unread{ver}\n    {tagline}"

    r, g, b = ACCENT
    dot = f"\x1b[38;2;{r};{g};{b}m●{RESET}"
    head = f"{BOLD}[{dot}{BOLD}] unread{RESET}{DIM}{ver}{RESET}"
    sub = f"{DIM}    {tagline}{RESET}"
    return f"{head}\n{sub}"


def print_banner(version: str = "", *, stream=None) -> None:
    stream = stream or sys.stdout
    print(banner(version, stream=stream), file=stream)
