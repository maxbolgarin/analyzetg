"""Website URL detection, normalization, and stable id derivation.

Pure stdlib. Runs from `cmd_analyze`'s detection branch before any
network code; cheap to call. The `is_telegram_url` helper duplicates the
hostname check from `enrich/link.py:_SKIP_HOSTS` so the website branch
never swallows a t.me link that should keep flowing through the
Telegram resolver.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Hosts that must NOT route to the website analyzer. `t.me` and friends
# go through the Telegram resolver (channels, posts, invites). Mirrors
# `enrich/link.py:_SKIP_HOSTS` so the two paths stay in sync.
_TELEGRAM_HOSTS = {
    "t.me",
    "telegram.me",
    "telegram.org",
    "telegra.ph",
}

# Tracking parameters dropped during normalization. We keep all other
# query params because many sites (search results, forum threads) put
# load-bearing state in them. List is conservative on purpose.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "ref_url",
    "_ga",
    "_gl",
}

_TRAILING_PUNCT = ".,;:!?)\"'»"


def is_website_url(s: str | None) -> bool:
    """True for anything that looks like an http(s) URL.

    Cheap parse — we don't validate that the host resolves, just that
    the scheme is one we can fetch with httpx. Returns False for empty,
    None, schemeless strings, and `ftp://` / `file://` etc.
    """
    if not s:
        return False
    if not s.startswith(("http://", "https://")):
        return False
    return bool((urlparse(s).hostname or "").strip())


def is_telegram_url(s: str | None) -> bool:
    """True if `s` is an http(s) URL pointing at Telegram's web surface.

    Used to keep `https://t.me/...` flowing through the Telegram
    resolver instead of being scraped as a generic webpage. Plain
    `@username` or numeric ids return False here — they're not URLs at
    all and the YouTube branch doesn't claim them either.
    """
    if not s:
        return False
    if not s.startswith(("http://", "https://")):
        return False
    host = (urlparse(s).hostname or "").lower()
    if host in _TELEGRAM_HOSTS:
        return True
    return host.endswith(".t.me")


def normalize_url(url: str) -> str:
    """Canonicalize for cache keying.

    Lowercases scheme + hostname, trims trailing punctuation, drops the
    fragment, drops common tracking params. The original URL is what
    we actually fetch — normalization affects `page_id` only, so a
    redirected URL's content still ends up under the user's typed form.
    """
    raw = url.rstrip(_TRAILING_PUNCT)
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower() or "https"
    netloc = (parsed.hostname or "").lower()
    # Preserve non-default ports; default 80/443 are dropped.
    if parsed.port and not (
        (scheme == "http" and parsed.port == 80) or (scheme == "https" and parsed.port == 443)
    ):
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        # Userinfo is rare in practice; preserve it verbatim if present.
        creds = parsed.username or ""
        if parsed.password:
            creds = f"{creds}:{parsed.password}"
        netloc = f"{creds}@{netloc}"

    # Drop tracking params; preserve param order otherwise.
    if parsed.query:
        kept = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in _TRACKING_PARAMS
        ]
        query = urlencode(kept)
    else:
        query = ""

    # Trailing slash is preserved on purpose: `/foo` and `/foo/` are
    # different documents on plenty of CMSes.
    return urlunparse((scheme, netloc, parsed.path, parsed.params, query, ""))


def page_id(normalized: str) -> str:
    """Stable 16-char id over the normalized URL — primary key in `website_pages`."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def domain_of(url: str) -> str:
    """Hostname with `www.` stripped, lowercased. `""` for unparseable URLs."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host
