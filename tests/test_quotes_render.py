"""Tests for the custom Rich renderer used by the `quotes` preset.

The renderer paints the blockquote bar magenta, quote text white, and
`@username` handles bold magenta — replacing Rich Markdown's default
single-color block_quote rendering. The saved markdown file is untouched;
only the terminal render swaps. These tests assert the styled segments
the user actually sees on screen.
"""

from __future__ import annotations

import io

from rich.console import Console

from unread.util.report_render import _render_quotes_inline, render_quotes_body


def _render_to_string(renderable, *, width: int = 100) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=width)
    console.print(renderable)
    return buf.getvalue()


def test_inline_author_gets_bold_magenta_and_quote_text_white() -> None:
    text = _render_quotes_inline("«Quote text.» — @lev_bzck, more text")
    out = _render_to_string(text)
    # Author handle: bold magenta = ESC[1;35m
    assert "\x1b[1;35m@lev_bzck\x1b[0m" in out
    # Quote text + surrounding prose: white = ESC[37m
    assert "\x1b[37m«Quote text.» — \x1b[0m" in out


def test_inline_citation_renders_as_clickable_link() -> None:
    text = _render_quotes_inline("[#12345](https://t.me/c/123/12345)")
    out = _render_to_string(text)
    # OSC 8 hyperlink wrapper around the citation label.
    assert "\x1b]8;" in out
    assert "https://t.me/c/123/12345" in out
    assert "#12345" in out


def test_body_renders_heading_bar_quote_author() -> None:
    body = "## Quotes\n\n> «Quote one.»\n> — @author1, [#1](https://t.me/c/1/1)\n"
    out = _render_to_string(render_quotes_body(body))
    # Heading: bold cyan
    assert "\x1b[1;36mQuotes\x1b[0m" in out
    # Blockquote bar: magenta
    assert "\x1b[35m▌ \x1b[0m" in out
    # Author handle keeps bold magenta even inside the blockquote
    assert "\x1b[1;35m@author1\x1b[0m" in out


def test_body_no_quotes_fallback_renders_plain_white() -> None:
    body = "## Quotes\n\nNo quotes worth saving were found.\n"
    out = _render_to_string(render_quotes_body(body))
    assert "\x1b[1;36mQuotes\x1b[0m" in out
    assert "\x1b[37mNo quotes worth saving were found.\x1b[0m" in out


def test_body_handles_bare_blockquote_separator_line() -> None:
    """A bare `>` line (no content) renders as just the bar."""
    body = "> «one»\n>\n> «two»\n"
    out = _render_to_string(render_quotes_body(body))
    # The bare `>` becomes a lone bar — distinct from the `▌ ` (bar + space)
    # used for content lines.
    assert "\x1b[35m▌\x1b[0m" in out
    assert "\x1b[35m▌ \x1b[0m«one»" in out or "\x1b[35m▌ \x1b[0m\x1b[37m«one»" in out
