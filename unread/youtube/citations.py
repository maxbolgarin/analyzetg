"""YouTube citation timestamp back-shift.

The LLM cites a moment in the video by the start-second of the
transcript segment that contained the quote. The chunker rounds those
boundaries down to the nearest segment start, so the actual quote
typically begins a few seconds *after* the cited offset — and a click
on the citation lands the listener mid-phrase. Subtracting a small
offset from every cited `?t=Ns` gives a short lead-in instead.

Default: 5 seconds, clamped at zero so a citation near the video start
can't go negative.
"""

from __future__ import annotations

import re

DEFAULT_OFFSET_SEC = 5

# Match `[label](URL?…t=Ns…)`. Labels can't contain `]`; URLs can't
# contain `)` per the markdown link grammar the LLM emits.
_LINK_WITH_T_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]*?[?&])t=(\d+)s([^)]*)\)")
# `MM:SS` or `HH:MM:SS`, optionally surrounded by square brackets the
# LLM sometimes adds. Used to spot clock-style labels we should rewrite
# alongside the URL.
_CLOCK_LABEL_RE = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?\s*$")


def _parse_clock_label(label: str) -> int | None:
    """Parse `MM:SS` / `HH:MM:SS` → seconds. Returns None on no match."""
    m = _CLOCK_LABEL_RE.match(label)
    if not m:
        return None
    a, b, c = m.groups()
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def _format_clock(seconds: int, *, with_hours: bool) -> str:
    if with_hours:
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def shift_citation_timestamps(report: str, *, offset_sec: int = DEFAULT_OFFSET_SEC) -> str:
    """Subtract `offset_sec` from every `?t=Ns` value in the report.

    When the citation's *label* is a clock-format timestamp matching
    the same second-value (e.g. `[08:15](…?t=495s)`), the label is
    reformatted to the shifted value so it stays consistent with the
    link target. Non-clock labels (`[#754]`, free text) are left as-is
    — only the URL shifts.

    Idempotence note: callers should run this exactly once per render.
    The analyzer's cache stores LLM output unshifted, so a cache hit on
    re-run reads the pre-shift text and applies the shift fresh —
    timestamps don't drift across runs.
    """
    if offset_sec <= 0 or not report:
        return report

    def _replace(match: re.Match[str]) -> str:
        label = match.group(1)
        url_prefix = match.group(2)
        old_seconds = int(match.group(3))
        url_suffix = match.group(4)

        new_seconds = max(0, old_seconds - offset_sec)
        if new_seconds == old_seconds:
            return match.group(0)

        new_label = label
        label_seconds = _parse_clock_label(label)
        if label_seconds is not None and label_seconds == old_seconds:
            new_label = _format_clock(new_seconds, with_hours=label.count(":") >= 2)

        return f"[{new_label}]({url_prefix}t={new_seconds}s{url_suffix})"

    return _LINK_WITH_T_RE.sub(_replace, report)
