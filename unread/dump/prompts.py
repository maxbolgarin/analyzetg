"""Mode picker for `unread dump <url>` (web vs YouTube).

When the user runs `unread dump <url>` without `--mode`, this prompts
for the artifact shape on a TTY and returns ``None`` in non-TTY runs so
the caller can raise a `BadParameter` instead of blocking on input.
"""

from __future__ import annotations

import sys
from typing import Literal

DumpKind = Literal["website", "youtube"]


def pick_dump_mode(kind: DumpKind, *, yes: bool) -> str | None:
    """Interactive mode picker.

    Returns the picked mode (``"text"``/``"full"`` for websites,
    ``"transcript"``/``"audio"``/``"video"`` for YouTube), or ``None``
    when stdin is not a TTY or ``yes=True`` (caller turns this into a
    ``BadParameter`` asking the user to pass ``--mode``).
    """
    if yes or not sys.stdin.isatty():
        return None

    from unread.i18n import t as _t
    from unread.util.prompt import Choice, select

    if kind == "website":
        prompt = _t("dump_pick_mode_web")
        choices = [
            Choice(value="text", label=_t("dump_choice_web_text")),
            Choice(value="full", label=_t("dump_choice_web_full")),
        ]
    elif kind == "youtube":
        prompt = _t("dump_pick_mode_yt")
        choices = [
            Choice(value="transcript", label=_t("dump_choice_yt_transcript")),
            Choice(value="audio", label=_t("dump_choice_yt_audio")),
            Choice(value="video", label=_t("dump_choice_yt_video")),
        ]
    else:
        raise ValueError(f"unknown dump kind: {kind!r}")

    try:
        picked = select(prompt, choices=choices)
    except KeyboardInterrupt:
        return None
    return str(picked) if picked else None
