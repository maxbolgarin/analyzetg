"""Website citation post-processing.

Web pages have no per-paragraph HTML anchors and the chunker merges
several article paragraphs into each `#N` segment, so per-citation
navigation isn't actually possible. We strip both forms — markdown
`[#N](page_url)` links AND the bare `#N` marker the LLM leaves in the
prose — so the saved report reads as plain narrative without phantom
citation artifacts the reader can't act on.

Other sources (Telegram, YouTube, local files) keep their citations
because those DO resolve to a specific message / timestamp / file URI.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# `[#N](URL)` markdown link.
_LINK_CITATION_RE = re.compile(r"\[#(\d+)\]\(([^)]+)\)")
# Bare-citation cluster: one or more `#N` chained with comma, hyphen,
# en/em dash or whitespace, with a leading separator so we also consume
# the space / comma the cluster sits on. Examples it eats:
#   "... основанием #1, #2."   -> "... основанием."
#   "... аргументации #1-#12"  -> "... аргументации"
#   "... входы #8, #9, #10."   -> "... входы."
_BARE_CITATION_CLUSTER_RE = re.compile(r"(?:[ \t]*[,\-–—][ \t]*|[ \t]+)#\d+(?:[ \t]*[,\-–—][ \t]*#\d+)*")


def _normalize(url: str) -> str:
    """Compare URLs ignoring trailing slash + existing fragment."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") if parts.path != "/" else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def strip_citations(report: str, *, base_url: str) -> str:
    """Remove every citation reference from the report.

    Two passes:
      1. Drop `[#N](base_url)` markdown wrappers — leaves text without
         the now-pointless link. URLs that don't match `base_url`
         (foreign references the LLM occasionally embeds in a Resources
         section) are left untouched.
      2. Drop bare `#N` clusters (`#3`, `#1, #2`, `#1–#12`, …) along
         with the surrounding separator so the prose reads naturally.
    """
    if not report:
        return report

    base_norm = _normalize(base_url)

    def _drop_link(match: re.Match[str]) -> str:
        if _normalize(match.group(2)) != base_norm:
            return match.group(0)
        return f"#{match.group(1)}"

    out = _LINK_CITATION_RE.sub(_drop_link, report)
    out = _BARE_CITATION_CLUSTER_RE.sub("", out)
    return out
