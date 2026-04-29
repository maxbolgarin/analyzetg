"""SSRF guard for outbound URL fetches.

The link enricher (``unread.enrich.link``) and the website analyzer
(``unread.website.content``) follow redirects on user-supplied URLs.
Without validation, a malicious page could redirect to:

* ``http://169.254.169.254/...`` — AWS / GCP / Azure instance-metadata
  endpoint, leaking IAM credentials.
* ``http://localhost:N`` / ``http://127.0.0.1:N`` — local services
  (admin panels, dev servers, dashboards).
* ``http://10.x.y.z`` / ``http://192.168.x.y`` — internal LAN hosts.

The fetched body is then summarized by an LLM and pasted into the
user's report — exfiltrating private data.

This module exposes:

* :func:`is_public_address` — DNS resolves a host and rejects any
  address in loopback / RFC1918 / link-local / unique-local /
  unspecified ranges.
* :class:`SafeAsyncClient` — drop-in replacement for ``httpx.AsyncClient``
  that validates the initial URL and every redirect hop. Refuses
  schemes other than ``http`` / ``https``.

Both link.py and website/content.py call into this module instead of
constructing ``httpx.AsyncClient`` directly.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from unread.util.logging import get_logger

log = get_logger(__name__)


class BlockedURLError(Exception):
    """Raised when a target URL resolves to a private / forbidden address."""


_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _addr_is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except (TypeError, ValueError):
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return False
    return not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def is_public_address(host: str) -> bool:
    """True iff ``host`` resolves *exclusively* to public addresses.

    A host that returns a mix (some public, some private) is treated
    as *not* public — DNS rebinding could otherwise smuggle a private
    target in. Resolution failure returns False so the caller short-
    circuits cleanly with a "blocked" log instead of a misleading
    successful fetch attempt.
    """
    # Direct IP literal — skip DNS.
    try:
        ipaddress.ip_address(host)
        return _addr_is_public(host)
    except (TypeError, ValueError):
        pass

    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return False
    # Explicit private TLDs by convention.
    if host.endswith(".local") or host.endswith(".internal"):
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not _addr_is_public(sockaddr[0]):
            return False
    return True


def _ensure_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"refusing non-http(s) scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise BlockedURLError(f"URL has no hostname: {url!r}")
    if not is_public_address(host):
        raise BlockedURLError(f"refusing fetch to non-public host {host!r}")


async def safe_get(
    url: str,
    *,
    timeout_sec: float,
    headers: Mapping[str, str] | None = None,
    max_redirects: int = 10,
) -> httpx.Response:
    """Issue a GET, validating the initial URL and every redirect hop.

    Implementation notes:

    * ``follow_redirects=False`` on the underlying client; we walk the
      chain manually so we can validate each hop before issuing the
      next request.
    * ``max_redirects`` defaults to 10. Tighter than httpx's 20 — anything
      past ~5 hops is almost always misbehaving infrastructure, and
      every extra hop is another chance for a redirect-to-private to slip
      past us.

    Raises :class:`BlockedURLError` when validation fails. Other errors
    propagate as the underlying httpx exceptions.
    """
    _ensure_safe_url(url)
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        headers=dict(headers or {}),
    ) as client:
        current = url
        for _ in range(max_redirects + 1):
            resp = await client.get(current)
            if resp.is_redirect:
                target = resp.headers.get("location")
                if not target:
                    return resp
                # Resolve relative redirects against current URL.
                next_url = str(httpx.URL(current).join(target))
                _ensure_safe_url(next_url)
                current = next_url
                continue
            return resp
        raise BlockedURLError(f"redirect chain exceeded {max_redirects} hops for {url!r}")


def safe_validate(url: str) -> None:
    """Assert ``url`` is safe to fetch; raise :class:`BlockedURLError` if not.

    Use when the caller wants to gate before delegating fetch to a
    third-party library (e.g. ``trafilatura.fetch_url``) that doesn't
    expose a redirect hook.
    """
    _ensure_safe_url(url)


__all__: tuple[str, ...] = (
    "BlockedURLError",
    "is_public_address",
    "safe_get",
    "safe_validate",
)


# Type guard — keep the ipaddress import effectively used in static checkers.
_ = ipaddress
_ = Any
