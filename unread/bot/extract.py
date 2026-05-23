"""Pull a TL;DR section out of an analyze report.

Reports from `cmd_analyze_*` always start with a frontmatter block and
a `## TL;DR` section (every preset in `presets/<lang>/*.md` opens the
report skeleton with that heading — language-independent). We extract
its body so the bot can ship it as the inline message text alongside
the full-report attachment.
"""

from __future__ import annotations

import re

# Match `## TL;DR` (case-insensitive, optional trailing punctuation).
# Locked to "TL;DR" / "TLDR" because every shipped preset uses one of
# those literal headings regardless of report language — the LLM
# preserves the Latin string.
_TLDR_HEADING_RE = re.compile(r"^\s*##\s+(?:TL;DR|TLDR)\b.*$", re.IGNORECASE)
_ANY_H2_RE = re.compile(r"^\s*##\s+\S")


def extract_tldr(md_text: str) -> str | None:
    """Return the body text under the first `## TL;DR` heading.

    Returns None when the report has no TL;DR section (e.g. a custom
    preset that uses a different skeleton). The caller decides what
    to show in that case.
    """
    lines = md_text.splitlines()
    in_tldr = False
    collected: list[str] = []
    for line in lines:
        if not in_tldr:
            if _TLDR_HEADING_RE.match(line):
                in_tldr = True
            continue
        # We're inside the TL;DR block. Stop at the next `## ` heading.
        if _ANY_H2_RE.match(line):
            break
        collected.append(line)
    body = "\n".join(collected).strip()
    return body or None
