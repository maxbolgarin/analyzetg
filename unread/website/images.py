"""Image extraction + download for `unread dump <url> --mode=full`.

Workflow:

1. :func:`extract_inlined_images` walks the article body for ``<img>``
   tags and returns ``[(idx, absolute_url), ...]``. Data URIs and
   tracking pixels are skipped at the source.
2. :func:`download_inlined_images` fetches each URL via the same SSRF
   guard the page fetch uses (:func:`unread.util.safe_fetch.safe_get`)
   and writes ``img-N.<ext>`` to a destination directory. Returns the
   ``(idx, local_path)`` mapping.
3. :func:`render_image_section` produces a markdown ``## Images``
   section linking each saved file. We don't try to splice images
   inline because trafilatura's text output already stripped the
   anchors — keeping the rewrite at the page footer keeps the markdown
   readable and the implementation honest.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from unread.config import Settings
from unread.util.logging import get_logger
from unread.util.safe_fetch import BlockedURLError, safe_get

log = get_logger(__name__)

_ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

_HTTP_SCHEMES = {"http", "https"}


async def extract_inlined_images(
    html: str,
    base_url: str,
    *,
    max_images: int,
) -> list[tuple[int, str, str]]:
    """Return ``[(index, absolute_url, alt_text), ...]`` from the article body.

    Walks ``<article>`` / ``<main>`` / ``<body>`` for ``<img>`` tags.
    Skips data URIs and obvious tracking pixels (``width=1`` AND
    ``height=1``). Caps at ``max_images``.
    """
    if max_images <= 0 or not html:
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("website.images.bs4_missing")
        return []

    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("article") or soup.find("main") or soup.body or soup
    out: list[tuple[int, str, str]] = []
    idx = 0
    for tag in container.find_all("img"):
        src = (tag.get("src") or tag.get("data-src") or "").strip()
        if not src:
            continue
        if src.startswith("data:"):
            continue
        try:
            w = int(str(tag.get("width") or "0").strip().rstrip("px") or 0)
            h = int(str(tag.get("height") or "0").strip().rstrip("px") or 0)
        except ValueError:
            w = h = 0
        if 0 < w <= 1 and 0 < h <= 1:
            continue
        absolute = urljoin(base_url, src)
        scheme = urlparse(absolute).scheme.lower()
        if scheme not in _HTTP_SCHEMES:
            continue
        alt = (tag.get("alt") or "").strip()
        idx += 1
        out.append((idx, absolute, alt))
        if len(out) >= max_images:
            break
    return out


async def download_inlined_images(
    images: list[tuple[int, str, str]],
    dest_dir: Path,
    *,
    settings: Settings,
) -> list[tuple[int, Path, str]]:
    """Download each image into ``dest_dir`` as ``img-N.<ext>``.

    Returns ``[(index, local_path, alt_text), ...]`` for the images
    that downloaded successfully. Failures (blocked URL, non-image
    Content-Type, network error) are logged at WARN and silently
    skipped — a partial dump is more useful than a failed one.
    """
    if not images:
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    cfg = settings.website
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "image/*,*/*;q=0.8",
    }
    saved: list[tuple[int, Path, str]] = []
    for idx, url, alt in images:
        try:
            resp = await safe_get(
                url,
                timeout_sec=cfg.fetch_timeout_sec,
                headers=headers,
                max_redirects=10,
            )
        except (BlockedURLError, httpx.HTTPError, httpx.InvalidURL) as e:
            log.warning("website.images.fetch_failed", url=url, err=str(e)[:200])
            continue
        if resp.status_code >= 400:
            log.warning("website.images.bad_status", url=url, status=resp.status_code)
            continue

        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        ext = _ALLOWED_TYPES.get(ctype)
        if ext is None:
            log.warning("website.images.unsupported_type", url=url, content_type=ctype)
            continue
        if len(resp.content) > cfg.max_html_bytes:
            log.warning("website.images.too_big", url=url, size=len(resp.content))
            continue

        local = dest_dir / f"img-{idx}{ext}"
        local.write_bytes(resp.content)
        saved.append((idx, local, alt))
    return saved


def render_image_section(saved: list[tuple[int, Path, str]], *, dir_name: str = "_files") -> str:
    """Markdown ``## Images`` block referencing each downloaded file.

    Empty list → empty string (caller appends nothing).
    """
    if not saved:
        return ""
    lines = ["## Images", ""]
    for idx, local, alt in saved:
        rel = f"{dir_name}/{local.name}"
        caption = alt or f"Image {idx}"
        lines.append(f"![{caption}]({rel})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
