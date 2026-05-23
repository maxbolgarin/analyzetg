"""Tests for `unread.bot.extract.extract_tldr`."""

from __future__ import annotations

from unread.bot.extract import extract_tldr


def test_extracts_tldr_body_between_headings():
    md = """\
---
**Chat:** Some Channel
**Cost:** $0.01
---
## TL;DR
This is the summary paragraph.
Second sentence in the same block.

## Главное
- bullet one
"""
    assert extract_tldr(md) == "This is the summary paragraph.\nSecond sentence in the same block."


def test_returns_none_when_no_tldr_section():
    md = "## Summary\nNo TL;DR here.\n"
    assert extract_tldr(md) is None


def test_handles_case_insensitive_and_tldr_variant():
    md = "## tldr\nbody text\n## Next\n"
    assert extract_tldr(md) == "body text"


def test_runs_to_end_of_doc_when_no_following_heading():
    md = "## TL;DR\nonly section in the doc\n"
    assert extract_tldr(md) == "only section in the doc"


def test_skips_frontmatter_and_finds_tldr():
    """The TL;DR helper must not be confused by an earlier `---` fence
    in the YAML-style frontmatter."""
    md = """\
---
Preset: summary
---

## TL;DR
the actual summary
## Decisions
- a
"""
    assert extract_tldr(md) == "the actual summary"


def test_preserves_inline_markdown_inside_tldr():
    """Bold / italic / citation links flow through unchanged for the
    inline TG reply to render."""
    md = """\
## TL;DR
**Important:** here's the [#1](https://example.com/1) bit.
## Next
"""
    assert extract_tldr(md) == "**Important:** here's the [#1](https://example.com/1) bit."
