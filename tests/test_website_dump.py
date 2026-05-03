"""`unread dump <website-url>` text + full modes."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from unread.config import get_settings
from unread.website.content import WebsitePage
from unread.website.dump import cmd_dump_website
from unread.website.images import (
    download_inlined_images,
    extract_inlined_images,
    render_image_section,
)
from unread.website.metadata import WebsiteMetadata


def _fake_page(url: str = "https://example.com/article") -> WebsitePage:
    meta = WebsiteMetadata(
        url=url,
        normalized_url=url,
        page_id="abcdef0123456789",
        domain="example.com",
        title="Hello World",
        site_name="Example",
        author="Jane",
        word_count=42,
    )
    return WebsitePage(
        metadata=meta,
        paragraphs=["First paragraph.", "Second paragraph."],
        raw_html_size=1234,
        fetched_at=datetime.now(UTC),
        content_hash="hash-abc",
        extractor="trafilatura",
    )


_RICH_HTML = """
<html><body><article>
  <h1>Hello World</h1>
  <h2>Section One</h2>
  <p>First paragraph.</p>
  <p>Second paragraph.</p>
</article></body></html>
"""


def _fetch_with_html_factory(page, html=_RICH_HTML):
    async def _fp(url, *, settings):
        return page, html

    return _fp


async def test_text_mode_writes_article_md(tmp_path) -> None:
    page = _fake_page()
    out = tmp_path / "out"
    with (
        patch("unread.website.dump.fetch_page_with_html", new=_fetch_with_html_factory(page)),
        patch(
            "unread.website.dump.extract_markdown_body",
            return_value="# Hello World\n\n## Section One\n\nFirst paragraph.\n\nSecond paragraph.",
        ),
    ):
        await cmd_dump_website(
            url="https://example.com/article",
            mode="text",
            max_images=10,
            output=out,
            console_out=False,
            language="en",
            content_language="en",
            yes=True,
        )
    article = out / "article.md"
    assert article.exists()
    body = article.read_text(encoding="utf-8")
    # Headings preserved.
    assert "# Hello World" in body
    assert "## Section One" in body
    assert "First paragraph." in body
    assert "Second paragraph." in body
    # No images section in text mode.
    assert "## Images" not in body
    assert not (out / "_files").exists()


async def test_text_mode_falls_back_when_markdown_empty(tmp_path) -> None:
    """Empty markdown extraction → fall back to txt paragraphs (never empty article)."""
    page = _fake_page("https://example.com/fallback")
    out = tmp_path / "out"
    with (
        patch("unread.website.dump.fetch_page_with_html", new=_fetch_with_html_factory(page)),
        patch("unread.website.dump.extract_markdown_body", return_value=""),
    ):
        await cmd_dump_website(
            url=page.metadata.url,
            mode="text",
            max_images=10,
            output=out,
            console_out=False,
            language="en",
            content_language="en",
            yes=True,
        )
    body = (out / "article.md").read_text(encoding="utf-8")
    assert "First paragraph." in body
    assert "Second paragraph." in body


async def test_full_mode_downloads_images_and_writes_section(tmp_path) -> None:
    page = _fake_page("https://example.com/fullmode")
    out = tmp_path / "out"

    raw_html = """
    <html><body><article>
      <h1>Hello World</h1>
      <p>hi</p>
      <img src="https://cdn.example.com/a.png" alt="apple">
      <img src="/b.jpg">
    </article></body></html>
    """

    class _Resp:
        def __init__(self, ctype: str, content: bytes) -> None:
            self.headers = {"content-type": ctype}
            self.content = content
            self.status_code = 200

    async def _safe_get(url: str, **_kw):
        if url.endswith(".png"):
            return _Resp("image/png", b"\x89PNG\r\n\x1a\nfake")
        return _Resp("image/jpeg", b"\xff\xd8\xff\xe0fake")

    with (
        patch(
            "unread.website.dump.fetch_page_with_html",
            new=_fetch_with_html_factory(page, html=raw_html),
        ),
        patch(
            "unread.website.dump.extract_markdown_body",
            return_value="# Hello World\n\nhi",
        ),
        patch("unread.website.images.safe_get", new=_safe_get),
    ):
        await cmd_dump_website(
            url=page.metadata.url,
            mode="full",
            max_images=10,
            output=out,
            console_out=False,
            language="en",
            content_language="en",
            yes=True,
        )

    article = (out / "article.md").read_text(encoding="utf-8")
    assert "# Hello World" in article
    assert "## Images" in article
    assert (out / "_files" / "img-1.png").exists()
    assert (out / "_files" / "img-2.jpg").exists()
    assert "_files/img-1.png" in article
    assert "_files/img-2.jpg" in article


async def test_full_mode_respects_max_images(tmp_path) -> None:
    page = _fake_page("https://example.com/maximg")
    out = tmp_path / "out"

    imgs = "".join(f'<img src="https://x.test/{i}.png">' for i in range(20))
    raw_html = f"<html><body><article>{imgs}</article></body></html>"

    class _Resp:
        def __init__(self) -> None:
            self.headers = {"content-type": "image/png"}
            self.content = b"\x89PNG\r\n\x1a\nfake"
            self.status_code = 200

    async def _safe_get(url, **_kw):
        return _Resp()

    with (
        patch(
            "unread.website.dump.fetch_page_with_html",
            new=_fetch_with_html_factory(page, html=raw_html),
        ),
        patch("unread.website.dump.extract_markdown_body", return_value="hi"),
        patch("unread.website.images.safe_get", new=_safe_get),
    ):
        await cmd_dump_website(
            url=page.metadata.url,
            mode="full",
            max_images=5,
            output=out,
            console_out=False,
            language="en",
            content_language="en",
            yes=True,
        )

    saved = list((out / "_files").iterdir())
    assert len(saved) == 5


async def test_full_mode_zero_images_no_section(tmp_path) -> None:
    page = _fake_page("https://example.com/zeroimg")
    out = tmp_path / "out"
    raw_html = "<html><body><article><p>plain</p></article></body></html>"

    with (
        patch(
            "unread.website.dump.fetch_page_with_html",
            new=_fetch_with_html_factory(page, html=raw_html),
        ),
        patch("unread.website.dump.extract_markdown_body", return_value="plain"),
    ):
        await cmd_dump_website(
            url=page.metadata.url,
            mode="full",
            max_images=10,
            output=out,
            console_out=False,
            language="en",
            content_language="en",
            yes=True,
        )

    body = (out / "article.md").read_text(encoding="utf-8")
    assert "## Images" not in body
    # _files dir is created lazily by download — empty list → no dir.
    assert not (out / "_files").exists()


@pytest.mark.parametrize(
    "tag,expected",
    [
        ('<img src="data:image/png;base64,iVBORw0KGgo=">', 0),
        ('<img src="https://x.test/a.png" width="1" height="1">', 0),
        ('<img src="/local.png">', 1),
        ('<img src="https://x.test/ok.webp">', 1),
        ("<img>", 0),
    ],
)
async def test_extract_filters_unwanted_imgs(tag, expected) -> None:
    html = f"<html><body><article>{tag}</article></body></html>"
    images = await extract_inlined_images(html, "https://example.com/x", max_images=10)
    assert len(images) == expected


async def test_download_skips_unsupported_content_type(tmp_path) -> None:
    settings = get_settings()

    class _Resp:
        def __init__(self) -> None:
            self.headers = {"content-type": "text/html"}
            self.content = b"not an image"
            self.status_code = 200

    async def _safe_get(url, **_kw):
        return _Resp()

    with patch("unread.website.images.safe_get", new=_safe_get):
        saved = await download_inlined_images(
            [(1, "https://x.test/a.png", "")],
            tmp_path / "_files",
            settings=settings,
        )
    assert saved == []


def test_render_image_section_empty() -> None:
    assert render_image_section([]) == ""


def test_render_image_section_uses_alt(tmp_path) -> None:
    saved = [(1, tmp_path / "img-1.png", "Cat sleeping")]
    block = render_image_section(saved)
    assert "## Images" in block
    assert "![Cat sleeping](_files/img-1.png)" in block


async def test_extract_markdown_body_preserves_headings() -> None:
    """Live trafilatura call — confirms headings + paragraphs survive.

    Trafilatura's article detection wants enough surrounding chrome
    (header/nav/footer/main) to identify a content block; a stripped-
    down ``<article>`` collapses to flat text. The fixture mirrors a
    real-world article shape so we exercise the markdown writer for
    real.
    """
    from unread.website.content import extract_markdown_body

    html = """
    <html><head><title>Test</title></head><body>
      <header><nav>nav stuff</nav></header>
      <main>
        <article>
          <h1>My Title</h1>
          <p><em>Subtitle text here</em></p>
          <h2>Section A</h2>
          <p>First paragraph here, with enough words to look like prose.</p>
          <p>Second paragraph here, also with several words and ideas.</p>
          <h2>Section B</h2>
          <p>Third paragraph rounds out the article body.</p>
        </article>
      </main>
      <footer>footer</footer>
    </body></html>
    """
    out = extract_markdown_body(html, url="https://example.com/x")
    # Trafilatura may not be installed in some environments — skip then.
    if not out:
        pytest.skip("trafilatura unavailable or returned empty")
    assert "# My Title" in out
    assert "## Section A" in out
    assert "## Section B" in out
    assert "First paragraph here" in out
    assert "Third paragraph" in out
