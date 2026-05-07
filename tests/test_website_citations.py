"""Tests for unread.website.citations — citation stripping."""

from __future__ import annotations

from unread.website.citations import strip_citations

_BASE = "https://example.com/page"


# ---- markdown-link stripping ----


def test_strip_drops_link_wrapper() -> None:
    out = strip_citations(f"text [#1]({_BASE}) more text follows.", base_url=_BASE)
    # Bare `#N` cluster gets dropped too in the second pass.
    assert "[#1]" not in out
    assert _BASE not in out


def test_strip_leaves_foreign_url_alone() -> None:
    src = "ext [#1](https://other.com/x) ref"
    out = strip_citations(src, base_url=_BASE)
    # Foreign markdown link stays — only matching base_url citations are stripped.
    assert "[#1](https://other.com/x)" in out


def test_strip_leaves_non_citation_link_alone() -> None:
    src = f"See [the docs]({_BASE}) for details."
    assert strip_citations(src, base_url=_BASE) == src


# ---- bare-citation cluster stripping ----


def test_strip_drops_trailing_single_citation() -> None:
    out = strip_citations("проверяются по тому, как работают в реальной жизни #4.", base_url=_BASE)
    assert out == "проверяются по тому, как работают в реальной жизни."


def test_strip_drops_chained_citations() -> None:
    out = strip_citations("сон и другие входы #8, #9.", base_url=_BASE)
    assert out == "сон и другие входы."


def test_strip_drops_long_chain() -> None:
    out = strip_citations("Decartes — Quine #1, #2, #3, #11, #12.", base_url=_BASE)
    assert out == "Decartes — Quine."


def test_strip_drops_em_dash_range() -> None:
    out = strip_citations("основные опоры аргументации #1–#12.", base_url=_BASE)
    assert out == "основные опоры аргументации."


def test_strip_drops_hyphen_range() -> None:
    out = strip_citations("range #1-#12 here", base_url=_BASE)
    assert out == "range here"


def test_strip_drops_space_separated_cluster() -> None:
    out = strip_citations("Items here #1 #2 #3 done.", base_url=_BASE)
    assert out == "Items here done."


def test_strip_handles_link_then_bare_in_same_text() -> None:
    src = f"Argues [#1]({_BASE}) and again #2 and finally #3, #4."
    out = strip_citations(src, base_url=_BASE)
    # Note: first pass turns [#1](URL) into bare #1, second pass strips it.
    assert "#" not in out
    assert _BASE not in out


# ---- edge cases ----


def test_strip_empty_input() -> None:
    assert strip_citations("", base_url=_BASE) == ""


def test_strip_no_citations() -> None:
    assert strip_citations("plain text.", base_url=_BASE) == "plain text."


def test_strip_does_not_match_hash_word_combos() -> None:
    """`#tag` (alphabetic) and `# 5` (separated) don't look like citations."""
    src = "Hashtag #foo here and # 7 spaced."
    out = strip_citations(src, base_url=_BASE)
    # `#foo` left alone (regex requires \d+ after #).
    assert "#foo" in out
    # `# 7` (with space) not a citation cluster — stays.
    assert "# 7" in out


def test_strip_handles_trailing_slash_in_link() -> None:
    out = strip_citations(f"see [#1]({_BASE}/) here", base_url=_BASE)
    assert _BASE not in out
    assert "[#1]" not in out


def test_strip_handles_query_string_in_link() -> None:
    base = "https://example.com/page?ref=foo"
    out = strip_citations(f"[#1]({base}) text", base_url=base)
    assert "[#1]" not in out
    assert base not in out


def test_strip_does_not_consume_intersentence_punct() -> None:
    """`#N` citations at end of one sentence shouldn't fuse with the next."""
    out = strip_citations("First sentence #1. Second sentence #2.", base_url=_BASE)
    assert out == "First sentence. Second sentence."
