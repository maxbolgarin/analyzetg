"""Fetch + extract main article text from a web page.

Two extractors:
  - **trafilatura** (preferred) — best-in-class article-body detection,
    drops nav/sidebar/footer, preserves headings + lists.
  - **BeautifulSoup** fallback — used when trafilatura isn't installed
    or returns nothing. Mirrors the extraction pattern from
    `enrich/link.py`, just without the 4000-char cap.

Both produce a normalized `(WebsiteMetadata, paragraphs[])` shape that
`commands.py` turns into synthetic `Message` rows for the analyzer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

try:
    import trafilatura
    from trafilatura.settings import use_config as _traf_use_config

    _HAS_TRAFILATURA = True
except ImportError:
    _HAS_TRAFILATURA = False
    trafilatura = None  # type: ignore[assignment]
    _traf_use_config = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
    BeautifulSoup = None  # type: ignore[assignment,misc]

from unread.config import Settings
from unread.util.logging import get_logger
from unread.website.metadata import WebsiteMetadata
from unread.website.urls import domain_of, normalize_url, page_id

log = get_logger(__name__)

# Each synthetic message body must stay below `formatter._BODY_CAP`
# (4000 chars) or the formatter truncates with "…". 3500 leaves
# headroom for any header decorations the formatter might add.
_SEGMENT_CHARS = 3500
_PARAGRAPH_RE = re.compile(r"\n{2,}")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Leading IETF / BCP-47 language tag normalization. Accepts `ru`,
# `ru-RU`, `ru_RU`, `zh-Hans`, `zh-Hant-TW`, etc. Returns the leading
# 2-letter ISO 639-1 code so downstream consumers (preset loader, i18n,
# settings UI) get a code they can render.
# Match a leading 2-3 letter language code followed by an explicit
# separator (`-`, `_`, whitespace) or end-of-string. Can't rely on `\b`
# because Python treats `_` as a word character — so `\b` doesn't fire
# between `en` and `_US`, and `en_US` would then match the wrong
# substring.
_LANG_TAG_HEAD_RE = re.compile(r"^\s*([A-Za-z]{2,3})(?:[-_]|\s|$)")
# Cheap regex fallback for `<html lang="..">` when BeautifulSoup is
# unavailable. Greedy on the opening `<html` so attributes that come
# before `lang` (e.g. `class`, `dir`, `xmlns`) don't mask the match.
_HTML_LANG_RE = re.compile(r"<html\b[^>]*\blang\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _normalize_lang_tag(tag: str | None) -> str | None:
    """BCP-47 / IETF tag → ISO 639-1 2-letter code, lowercased.

    Examples: ``"ru-RU"`` → ``"ru"``, ``"en_US"`` → ``"en"``,
    ``"zh-Hans"`` → ``"zh"``, ``"  RU  "`` → ``"ru"``,
    ``"eng"`` → None (3-letter ISO 639-2/3 not currently supported by
    the preset / i18n layer).
    """
    if not tag:
        return None
    m = _LANG_TAG_HEAD_RE.match(tag)
    if not m:
        return None
    code = m.group(1).lower()
    return code if len(code) == 2 else None


def _detect_html_language(html: str, *, url: str = "") -> str | None:
    """Detect the page language from raw HTML, walking the priority chain.

    Order (first non-empty wins):
      1. ``<html lang="...">`` — primary HTML5 mechanism, set by most CMS.
      2. ``<meta http-equiv="content-language" content="...">`` — older
         equivalent; still common on legacy CMS output.
      3. ``<meta property="og:locale" content="ru_RU">`` — Open Graph,
         used by social-share-aware sites (CMSes with SEO plugins).
      4. ``<link rel="alternate" hreflang="ru" href="..." />`` — only
         used when the alternate's ``href`` matches the page's own URL
         or the canonical link, otherwise the alternates list other
         language versions of the page (not the current one's language).

    Returns a 2-letter ISO 639-1 code, or None when no rule fires.
    Uses BeautifulSoup when available; falls back to a regex for the
    primary `<html lang>` rule when BS4 isn't installed.
    """
    if not html:
        return None
    if not _HAS_BS4:
        m = _HTML_LANG_RE.search(html)
        return _normalize_lang_tag(m.group(1)) if m else None

    try:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
    except Exception:  # pragma: no cover - extremely defensive
        return None

    # 1. <html lang="ru">
    if soup.html is not None and soup.html.get("lang"):
        code = _normalize_lang_tag(soup.html.get("lang"))
        if code:
            return code

    # 2. <meta http-equiv="content-language" content="ru">
    meta_http = soup.find(
        "meta",
        attrs={"http-equiv": lambda v: bool(v) and v.lower() == "content-language"},
    )
    if meta_http is not None:
        code = _normalize_lang_tag(meta_http.get("content"))
        if code:
            return code

    # 3. <meta property="og:locale" content="ru_RU">
    og_locale = soup.find("meta", attrs={"property": "og:locale"})
    if og_locale is not None:
        code = _normalize_lang_tag(og_locale.get("content"))
        if code:
            return code

    # 4. <link rel="alternate" hreflang="ru" href="...">
    # The hreflang list enumerates all language versions; we can only
    # use it to identify the CURRENT page's language by matching the
    # alternate's href against the canonical URL or the input URL.
    canonical = soup.find("link", attrs={"rel": "canonical"})
    canonical_href = (canonical.get("href") if canonical else "") or ""
    targets = {h for h in (canonical_href, url) if h}
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        hreflang = (link.get("hreflang") or "").strip()
        href = (link.get("href") or "").strip()
        if not hreflang or hreflang.lower() == "x-default" or not href:
            continue
        if targets and href in targets:
            code = _normalize_lang_tag(hreflang)
            if code:
                return code

    return None


def _content_language_from_header(value: str | None) -> str | None:
    """Parse RFC 7231 Content-Language header → first ISO 639-1 code.

    The header may carry multiple comma-separated tags
    (``"en, ru-RU"``); we take the first one and normalize it. Empty /
    malformed → None.
    """
    if not value:
        return None
    first = value.split(",", 1)[0]
    return _normalize_lang_tag(first)


class WebsiteFetchError(Exception):
    """Raised when fetch_page can't produce usable article text.

    Wrapped at the call site (commands.py) into a `typer.BadParameter`
    so the user gets a clean error instead of a stack trace. Message
    is intended for end-user display.
    """


@dataclass(slots=True)
class WebsitePage:
    metadata: WebsiteMetadata
    paragraphs: list[str]
    raw_html_size: int
    fetched_at: datetime
    content_hash: str
    extractor: str  # "trafilatura" | "beautifulsoup"


async def fetch_page(url: str, *, settings: Settings) -> WebsitePage:
    """HTTP GET + extract article text. Raises `WebsiteFetchError` on failure.

    The extracted text is segmented into ≤_SEGMENT_CHARS paragraph chunks
    (preferring blank-line boundaries, falling back to sentence breaks
    for runaway paragraphs). The result is capped to
    `settings.website.max_paragraphs` so a pathological page can't
    trigger thousands of synthetic messages.
    """
    page, _html = await fetch_page_with_html(url, settings=settings)
    return page


async def fetch_page_with_html(url: str, *, settings: Settings) -> tuple[WebsitePage, str]:
    """Same as :func:`fetch_page` but also returns the raw HTML.

    The HTML is otherwise discarded inside ``fetch_page``.
    ``unread dump <url>`` uses it to run a second-pass markdown
    extraction (preserves heading / paragraph structure) and to walk
    ``<img>`` tags for ``--mode=full``, all without paying for a second
    HTTP round-trip.
    """
    cfg = settings.website
    normalized = normalize_url(url)
    html, raw_size, header_lang = await _http_get(
        url, cfg.fetch_timeout_sec, cfg.user_agent, cfg.max_html_bytes
    )
    return _extract_page_from_html(
        html,
        url=url,
        normalized=normalized,
        raw_size=raw_size,
        settings=settings,
        header_language=header_lang,
    ), html


def _extract_page_from_html(
    html: str,
    *,
    url: str,
    normalized: str,
    raw_size: int,
    settings: Settings,
    header_language: str | None = None,
) -> WebsitePage:
    """Run extractor + segmentation. Pure post-fetch logic — no I/O.

    ``header_language`` is the parsed RFC 7231 ``Content-Language``
    response header (when the server sent it). It feeds the priority
    chain that resolves the final ``metadata.language``.
    """
    cfg = settings.website
    metadata: WebsiteMetadata | None = None
    text: str = ""
    extractor_used = ""

    if _HAS_TRAFILATURA:
        try:
            metadata, text = _extract_with_trafilatura(html, url=url, normalized=normalized)
            extractor_used = "trafilatura"
        except Exception as e:
            # Don't crash on extractor bugs — fall through to BS4.
            log.warning("website.extract.trafilatura_failed", url=url, err=str(e)[:200])
            metadata = None
            text = ""

    if not text:
        if not _HAS_BS4:
            raise WebsiteFetchError(
                "Cannot extract page content: neither `trafilatura` nor `beautifulsoup4` "
                "is installed. Run `uv tool install --editable . --reinstall` to fix."
            )
        metadata, text = _extract_with_bs4(html, url=url, normalized=normalized)
        extractor_used = "beautifulsoup"

    if not text.strip():
        raise WebsiteFetchError(_explain_empty_extraction(url, html, raw_size))

    assert metadata is not None  # one of the branches above sets it

    # Resolve the final language with the documented priority chain
    # (highest-confidence signal wins, and we ALWAYS run the HTML pass —
    # trafilatura's metadata.language is hit-and-miss on real pages):
    #   1. HTTP Content-Language response header
    #   2. <html lang>, <meta http-equiv>, <meta og:locale>, <link hreflang>
    #   3. Whatever trafilatura/BS4 already put in metadata.language
    # URL-based inference and LLM-side fallback live in the analyzer
    # (commands.cmd_analyze_website + analyzer.prompts) — they're a
    # layer below this and only fire when this chain returns empty.
    detected = header_language or _detect_html_language(html, url=url)
    if detected:
        metadata.language = detected
    elif metadata.language:
        # Trafilatura / BS4 already set something — normalize it so a
        # downstream comparison `metadata.language == "ru"` works even
        # when trafilatura returned `"ru-RU"`.
        metadata.language = _normalize_lang_tag(metadata.language) or metadata.language.lower()

    paragraphs = _segment_paragraphs(text, max_chars=_SEGMENT_CHARS)
    if cfg.max_paragraphs > 0 and len(paragraphs) > cfg.max_paragraphs:
        log.warning(
            "website.extract.paragraphs_capped",
            url=url,
            kept=cfg.max_paragraphs,
            dropped=len(paragraphs) - cfg.max_paragraphs,
        )
        paragraphs = paragraphs[: cfg.max_paragraphs]

    word_count = sum(len(p.split()) for p in paragraphs)
    metadata.word_count = word_count

    joined = "\n\n".join(paragraphs)
    content_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]

    return WebsitePage(
        metadata=metadata,
        paragraphs=paragraphs,
        raw_html_size=raw_size,
        fetched_at=datetime.now(UTC),
        content_hash=content_hash,
        extractor=extractor_used,
    )


def extract_markdown_body(html: str, *, url: str) -> str:
    """Trafilatura article extraction in markdown — preserves headings + paragraphs.

    Used by ``unread dump <url>`` so the saved ``article.md`` keeps
    ``# Title``, ``## Section``, paragraph breaks, and inline links.
    Returns ``""`` on failure (caller can fall back to txt paragraphs).

    Why this is separate from :func:`fetch_page`'s txt path: the LLM
    side of the pipeline wants heading-mark-free, segmented chunks
    (cheaper tokens, simpler analysis). Dump wants the inverse — a
    human-readable markdown document. Running both extractions on the
    same HTML costs nothing measurable next to the network fetch.
    """
    if not _HAS_TRAFILATURA:
        return ""
    try:
        cfg = _traf_use_config()  # type: ignore[misc]
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")
        out = trafilatura.extract(  # type: ignore[union-attr]
            html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_recall=True,
            config=cfg,
        )
        return out or ""
    except Exception as e:
        log.warning("website.extract.markdown_failed", url=url, err=str(e)[:200])
        return ""


async def _http_get(
    url: str,
    timeout_sec: int,
    user_agent: str,
    max_bytes: int,
) -> tuple[str, int, str | None]:
    """Fetch raw HTML or raise `WebsiteFetchError`.

    Returns ``(text, raw_size, content_language)`` where the third
    element is the RFC 7231 ``Content-Language`` response header parsed
    down to an ISO 639-1 code, or None when the server didn't send it
    (which is most servers — `<html lang>` is more commonly used).
    """
    from unread.util.safe_fetch import BlockedURLError, safe_get

    try:
        # SSRF guard: validate the initial URL and every redirect hop
        # so a malicious page can't bounce us to AWS metadata, local
        # admin panels, or LAN hosts. The fetched body is fed to the
        # LLM and into the user's report; a leak there exfiltrates.
        resp = await safe_get(
            url,
            timeout_sec=timeout_sec,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.7,ru;q=0.3",
            },
            max_redirects=10,
        )
    except BlockedURLError as e:
        raise WebsiteFetchError(f"Refused to fetch {url!r}: {e}") from e
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise WebsiteFetchError(f"Fetch failed: {e}") from e

    if resp.status_code >= 400:
        raise WebsiteFetchError(f"HTTP {resp.status_code} for {url!r}.")
    ctype = resp.headers.get("content-type", "")
    if "text/html" not in ctype and "application/xhtml+xml" not in ctype and "text/plain" not in ctype:
        raise WebsiteFetchError(f"Unexpected content-type {ctype!r} for {url!r}.")

    header_lang = _content_language_from_header(resp.headers.get("content-language"))

    raw_size = len(resp.content)
    text = resp.text
    if raw_size > max_bytes:
        log.warning("website.fetch.truncated", url=url, raw_size=raw_size, cap=max_bytes)
        text = text[:max_bytes]
        raw_size = max_bytes
    return text, raw_size, header_lang


def _extract_with_trafilatura(html: str, *, url: str, normalized: str) -> tuple[WebsiteMetadata, str]:
    """trafilatura article-body extraction + bibliographic metadata.

    Returns `(metadata, plain-text-with-blank-line-paragraphs)`. We
    request `output_format="txt"` and `include_comments=False` —
    cleanest for analysis. Headings are kept as their own lines with
    blank-line separators, which feeds `_segment_paragraphs` cleanly.
    """
    cfg = _traf_use_config()  # type: ignore[misc]
    # Disable trafilatura's URL-de-duplication cache: we don't want a
    # process-global LRU swallowing legitimate re-fetches across runs.
    cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")
    text = trafilatura.extract(  # type: ignore[union-attr]
        html,
        url=url,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_recall=True,
        config=cfg,
    )
    if not text:
        return _empty_metadata(url, normalized), ""

    raw_meta = trafilatura.extract_metadata(html, default_url=url)  # type: ignore[union-attr]
    title: str | None = None
    author: str | None = None
    published: str | None = None
    site_name: str | None = None
    language: str | None = None
    if raw_meta is not None:
        title = (raw_meta.title or None) if hasattr(raw_meta, "title") else None
        author = (raw_meta.author or None) if hasattr(raw_meta, "author") else None
        published = (raw_meta.date or None) if hasattr(raw_meta, "date") else None
        site_name = (raw_meta.sitename or None) if hasattr(raw_meta, "sitename") else None
        language = (raw_meta.language or None) if hasattr(raw_meta, "language") else None

    domain = domain_of(url)
    if not site_name:
        site_name = domain or None

    return (
        WebsiteMetadata(
            url=url,
            normalized_url=normalized,
            page_id=page_id(normalized),
            domain=domain,
            title=title,
            site_name=site_name,
            author=author,
            published=published,
            language=language,
        ),
        text,
    )


def _extract_with_bs4(html: str, *, url: str, normalized: str) -> tuple[WebsiteMetadata, str]:
    """BeautifulSoup fallback: strip noise, join body paragraphs.

    Less precise than trafilatura — tends to keep some chrome — but
    available everywhere and good enough for many simple article pages.
    """
    soup = BeautifulSoup(html, "html.parser")  # type: ignore[misc]
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript", "header", "form"]):
        tag.decompose()

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    site_name: str | None = None
    og_site = soup.find("meta", attrs={"property": "og:site_name"})
    if og_site and og_site.get("content"):
        site_name = og_site.get("content").strip() or None

    author: str | None = None
    meta_author = soup.find("meta", attrs={"name": "author"})
    if meta_author and meta_author.get("content"):
        author = meta_author.get("content").strip() or None

    published: str | None = None
    for sel in (
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publish_date"}),
        ("meta", {"itemprop": "datePublished"}),
    ):
        m = soup.find(sel[0], attrs=sel[1])
        if m and m.get("content"):
            published = m.get("content").strip() or None
            break

    language: str | None = None
    if soup.html and soup.html.get("lang"):
        language = (soup.html.get("lang") or "").strip().split("-", 1)[0] or None

    # Prefer the most-specific article container if present.
    article = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs: list[str] = []
    for el in article.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        text = el.get_text(" ", strip=True)
        if text:
            paragraphs.append(text)
    body_text = "\n\n".join(paragraphs)

    # Whole-body fallback: many modern sites use only <div>/<span> with no
    # semantic tags, so the targeted-tag pass above produces nothing. Fall
    # back to the full body text — noisy, but still better than zero output.
    if not body_text.strip():
        whole = (soup.body or soup).get_text("\n", strip=True)
        body_text = re.sub(r"\n{3,}", "\n\n", whole)

    domain = domain_of(url)
    if not site_name:
        site_name = domain or None

    return (
        WebsiteMetadata(
            url=url,
            normalized_url=normalized,
            page_id=page_id(normalized),
            domain=domain,
            title=title,
            site_name=site_name,
            author=author,
            published=published,
            language=language,
        ),
        body_text,
    )


def _empty_metadata(url: str, normalized: str) -> WebsiteMetadata:
    domain = domain_of(url)
    return WebsiteMetadata(
        url=url,
        normalized_url=normalized,
        page_id=page_id(normalized),
        domain=domain,
        site_name=domain or None,
    )


# Markers whose presence in the raw HTML strongly suggests a JS-rendered
# SPA: the page boots an empty container and lets a JS bundle paint the
# real content client-side. trafilatura/BS4 only see the bootstrap shell.
_SPA_MARKERS = (
    "<noscript>",
    "<md-root",
    "<app-root",
    "<ng-app",
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    "data-reactroot",
)


def _explain_empty_extraction(url: str, html: str, raw_size: int) -> str:
    """Build a user-facing error message for the all-extractors-empty case.

    Distinguishes "site requires JS" (very likely an SPA) from "extractor
    couldn't find the article body" so the user knows whether to retry
    with a different URL or just give up. The hint we print is what we'd
    type ourselves if we hit this in the wild.
    """
    short = raw_size < 50_000
    looks_spa = any(m in html for m in _SPA_MARKERS)
    if short and looks_spa:
        return (
            f"Page at {url!r} appears to be a JavaScript-rendered single-page app — "
            f"the server returned only ~{raw_size:,} bytes of bootstrapping markup with "
            "no readable text. unread fetches raw HTML only (no JS engine), so this "
            "kind of page can't be analyzed. Try a static article URL instead, or paste "
            "the article text via a different route."
        )
    if short:
        return (
            f"Page at {url!r} returned only ~{raw_size:,} bytes and no readable text — "
            "likely a redirect, login wall, or near-empty landing page. Check the URL "
            "in a browser, or pass the article URL directly."
        )
    return (
        f"Extracted no readable text from {url!r} despite a ~{raw_size:,}-byte response. "
        "The page may be heavily scripted, paywalled, or use unusual markup. If you can "
        "view it in a browser, try a more specific article URL."
    )


def _segment_paragraphs(text: str, *, max_chars: int = _SEGMENT_CHARS) -> list[str]:
    """Split extracted text into ≤max_chars chunks.

    Strategy:
      1. Split on blank lines (the natural paragraph break trafilatura
         and the BS4 fallback both produce).
      2. For each paragraph, append to current segment if it fits;
         otherwise flush and start a new one.
      3. If a single paragraph is longer than max_chars, split it on
         sentence boundaries; pure hard-cut as last resort. Mirrors
         `youtube/commands.py:_segment_transcript`.
    """
    text = text.strip()
    if not text:
        return []

    raw_paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text)]
    raw_paragraphs = [p for p in raw_paragraphs if p]
    if not raw_paragraphs:
        return []

    out: list[str] = []
    buf = ""
    for para in raw_paragraphs:
        if len(para) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_split_long(para, max_chars=max_chars))
            continue
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            buf = para
    if buf:
        out.append(buf)
    return out


def _split_long(text: str, *, max_chars: int) -> list[str]:
    """Split a single oversize paragraph on sentence breaks, then hard-cut."""
    sentences = _SENTENCE_END.split(text)
    out: list[str] = []
    buf = ""
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        candidate = (buf + " " + s) if buf else s
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(s) <= max_chars:
            buf = s
            continue
        tail = s
        while len(tail) > max_chars:
            out.append(tail[:max_chars])
            tail = tail[max_chars:]
        buf = tail
    if buf:
        out.append(buf)
    return out
