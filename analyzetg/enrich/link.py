"""Link enricher: extract URLs from message text, fetch, summarize, cache.

Runs per unique URL (not per message), so a link shared in 50 messages costs
one fetch + one summary. Cache key is sha256(url)[:24], normalized by
stripping fragments and tracking params. `t.me/*` URLs are skipped — they're
already first-class Telegram message references and the analyzer handles
them via the per-message link template.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

import httpx

# beautifulsoup4 is a soft dependency: if it isn't installed (common right
# after an upgrade where the user hasn't run `uv tool install --editable .
# --reinstall` yet), link enrichment degrades to a no-op with a warning
# rather than breaking every `atg analyze` at import time.
try:
    from bs4 import BeautifulSoup

    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False
    BeautifulSoup = None  # type: ignore[assignment,misc]

from analyzetg.analyzer.openai_client import build_messages, chat_complete, make_client
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.base import EnrichResult
from analyzetg.models import Message
from analyzetg.util.logging import get_logger

log = get_logger(__name__)

# Match bare URLs in message text. Simple and permissive; trailing punctuation
# is trimmed at the end. URLs wrapped in markdown `[title](url)` are matched
# via the same regex because we scan for `http(s)://...` start.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCT = ".,;:!?)\"'»"

_SKIP_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.org",
    "telegra.ph",  # already plain-text, not worth a fetch+summary round-trip
}

_SYSTEM_PROMPT = (
    "Ты составляешь очень короткое (1–2 предложения) резюме содержимого веб-страницы"
    " для анализа Telegram-чата. Пиши на том же языке, что и страница/чат."
    " Без маркетинга, без воды. Только суть: о чём страница, главный факт/тезис."
)


def _normalize_url(url: str) -> str:
    """Strip trailing punctuation, fragment, and common tracking params.

    Normalization is conservative — it affects the cache key but not what's
    actually fetched (we fetch the original URL so redirects work).
    """
    url = url.rstrip(_TRAILING_PUNCT)
    parsed = urlparse(url)
    # Drop fragment; keep everything else (query may be load-bearing).
    return urlunparse(parsed._replace(fragment=""))


def extract_urls(text: str | None) -> list[str]:
    """Return unique URLs from message text, preserving first-seen order.

    Filters out `t.me/*` and similar internal hosts — those are Telegram
    message references, handled elsewhere. Deduped across the text so the
    same URL pasted twice only enqueues one fetch.
    """
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _URL_RE.finditer(text):
        raw = m.group(0)
        normalized = _normalize_url(raw)
        host = urlparse(normalized).hostname or ""
        host = host.lower()
        if host in _SKIP_HOSTS or host.endswith(".t.me"):
            continue
        if normalized not in seen:
            seen[normalized] = None
    return list(seen.keys())


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _clean_fetched_text(html: str) -> tuple[str | None, str]:
    """Return (title, readable-ish text) from an HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove noise.
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript"]):
        tag.decompose()

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og = soup.find("meta", attrs={"property": "og:description"})
    og_desc = (og.get("content") or "").strip() if og else ""
    body_text = soup.get_text(" ", strip=True)
    # Prefer og:description as a lead-in, then append the body excerpt.
    parts = [p for p in (og_desc, body_text) if p]
    combined = "\n\n".join(parts)
    return title, combined[:4000]


async def _fetch(url: str, timeout_sec: int) -> str | None:
    try:
        async with httpx.AsyncClient(
            timeout=timeout_sec,
            follow_redirects=True,
            headers={"User-Agent": "analyzetg-link-enricher/0.1 (+https://github.com/maxbolgarin/analyzetg)"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                log.debug("enrich.link.http_error", url=url, status=resp.status_code)
                return None
            ctype = resp.headers.get("content-type", "")
            if "text/html" not in ctype and "text/plain" not in ctype:
                log.debug("enrich.link.non_html", url=url, ctype=ctype)
                return None
            # Cap at 2 MB — bail on anything larger to avoid hanging on big pages.
            if len(resp.content) > 2_000_000:
                return resp.text[:2_000_000]
            return resp.text
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        log.debug("enrich.link.fetch_error", url=url, err=str(e)[:200])
        return None


async def enrich_url(
    url: str,
    *,
    repo: Repo,
    model: str | None = None,
    timeout_sec: int | None = None,
    skip_domains: list[str] | None = None,
) -> EnrichResult | None:
    """Fetch + summarize a single URL. Caches per normalized URL hash.

    `skip_domains` is matched as case-insensitive substring of the hostname,
    so `"twitter.com"` skips both twitter.com and x.twitter.com.
    """
    settings = get_settings()
    normalized = _normalize_url(url)
    host = (urlparse(normalized).hostname or "").lower()
    if skip_domains:
        for d in skip_domains:
            if d.lower() in host:
                return None

    # Mirror the `FfmpegMissing` pattern: a missing optional library should
    # skip this enricher with a clear log line, not take down the run.
    if not _HAS_BS4:
        log.warning(
            "enrich.link.lib_missing",
            lib="beautifulsoup4",
            hint="run `uv tool install --editable . --reinstall`",
        )
        return None

    h = _url_hash(normalized)
    cached = await repo.get_link_enrichment(h)
    if cached:
        summary = cached.get("summary") or ""
        return EnrichResult(
            kind="link_summary",
            content=summary,
            model=cached.get("model"),
            cache_hit=True,
        )

    timeout = timeout_sec if timeout_sec is not None else settings.enrich.link_fetch_timeout_sec
    html = await _fetch(normalized, timeout)
    if html is None:
        return None
    title, body = _clean_fetched_text(html)
    if not body.strip():
        return None

    used_model = model or settings.enrich.link_model or settings.openai.filter_model_default
    oai = make_client()
    user_text = (
        f"URL: {normalized}\n"
        f"{'Заголовок: ' + title if title else ''}\n\n"
        f"Содержимое (фрагмент):\n{body}\n\n"
        "Сводка:"
    )
    messages = build_messages(_SYSTEM_PROMPT, "", user_text)
    res = await chat_complete(
        oai,
        repo=repo,
        model=used_model,
        messages=messages,
        max_tokens=200,
        context={"phase": "enrich_link", "url_hash": h},
    )
    summary = (res.text or "").strip()
    if not summary:
        return None

    await repo.put_link_enrichment(
        h,
        normalized,
        summary,
        title=title,
        model=used_model,
        cost_usd=float(res.cost_usd or 0.0),
    )
    return EnrichResult(
        kind="link_summary",
        content=summary,
        cost_usd=float(res.cost_usd or 0.0),
        model=used_model,
    )


async def enrich_message_links(
    msg: Message,
    *,
    repo: Repo,
    model: str | None = None,
    timeout_sec: int | None = None,
    skip_domains: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Extract + enrich every unique URL in `msg.text`.

    Returns `[(url, summary), ...]` and also attaches the list to
    `msg.link_summaries` so the formatter can render them inline.
    Empty list means no extractable links (not an error).
    """
    urls = extract_urls(msg.text)
    if not urls:
        return []
    pairs: list[tuple[str, str]] = []
    for url in urls:
        res = await enrich_url(
            url,
            repo=repo,
            model=model,
            timeout_sec=timeout_sec,
            skip_domains=skip_domains,
        )
        if res is None:
            continue
        pairs.append((url, res.content))
    if pairs:
        msg.link_summaries = pairs
    return pairs
