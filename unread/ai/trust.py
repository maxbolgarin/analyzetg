"""Trust check for ``settings.ai.base_url`` overrides.

When a user (or, worse, a typo / social-engineered config) sets
``ai.base_url`` to an unfamiliar host, every request — carrying the
upstream provider's API key as a Bearer token — is sent to that host.
A typo like ``https://api.openai.com.attacker.tld/v1`` would silently
exfiltrate the user's OpenAI key.

This module owns the per-provider allowlist of trusted hosts. When a
``base_url`` resolves to a host outside the active provider's
allowlist, :func:`enforce_base_url_trust` raises
:class:`ProviderUnavailableError` with copy that names the offending
host and tells the user how to opt in (``ai.base_url_trusted = true``).

Localhost / loopback / RFC1918 / link-local addresses are always
accepted as "trusted" — those are by definition not external
exfiltration paths, and self-hosted servers (Ollama, LM Studio, vLLM)
need to work without the opt-in flag.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from unread.ai.providers import ProviderUnavailableError

# Per-provider canonical hosts we accept by default. Subdomains of
# these (e.g. ``oai.openai.com``) are also trusted via suffix match.
_TRUSTED_HOSTS: dict[str, frozenset[str]] = {
    "openai": frozenset({"api.openai.com"}),
    "openrouter": frozenset({"openrouter.ai"}),
    "anthropic": frozenset({"api.anthropic.com"}),
    "google": frozenset({"generativelanguage.googleapis.com", "aiplatform.googleapis.com"}),
    # `local` accepts any host — it's by design pointing at user infra.
    "local": frozenset(),
}

_LOCAL_HOSTNAMES: frozenset[str] = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


def _hostname_of(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.hostname.lower() if parsed.hostname else None


def _is_local_or_private(host: str) -> bool:
    """True for loopback / RFC1918 / link-local / unique-local addresses.

    Resolves the hostname and checks every returned address — if any
    is private/loopback we accept the host. Resolution failure (no DNS,
    typo) is treated as not-private so the upstream connect fails fast
    with the real error rather than us masking it.
    """
    if host in _LOCAL_HOSTNAMES:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        # mDNS / private-DNS conventions
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (TypeError, ValueError):
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return True
    return False


def _matches_trusted(host: str, trusted: frozenset[str]) -> bool:
    return any(host == t or host.endswith("." + t) for t in trusted)


def enforce_base_url_trust(provider: str, settings) -> None:  # type: ignore[no-untyped-def]
    """Raise if ``settings.ai.base_url`` is set to an untrusted host.

    No-ops when ``base_url`` is empty (falls back to per-provider
    defaults), when the provider is ``local`` (the user is explicitly
    pointing at their own infra), or when ``ai.base_url_trusted`` is
    True (the user has acknowledged the override).

    OpenRouter and Local resolve their own ``base_url`` even without
    ``ai.base_url`` set, so this function only inspects the global
    ``ai.base_url`` override — the provider's built-in default is
    always trusted.
    """
    if provider == "local":
        return
    custom = (settings.ai.base_url or "").strip()
    if not custom:
        return
    if getattr(settings.ai, "base_url_trusted", False):
        return
    host = _hostname_of(custom)
    if not host:
        raise ProviderUnavailableError(
            f"`ai.base_url` is set to {custom!r} but no hostname could be parsed. "
            "Either set a real URL or clear `ai.base_url`."
        )
    if _is_local_or_private(host):
        return
    trusted = _TRUSTED_HOSTS.get(provider, frozenset())
    if _matches_trusted(host, trusted):
        return
    allowed = ", ".join(sorted(trusted)) or "(none — pick `local` provider for self-hosted)"
    raise ProviderUnavailableError(
        f"`ai.base_url` points at {host!r}, which is outside the trusted-host "
        f"allowlist for provider {provider!r} ({allowed}). Sending your API key "
        f"to an unverified host could leak it. To override, set `ai.base_url_trusted = true` "
        f"in config.toml or via `unread settings`. For self-hosted servers, switch "
        f"`ai.provider` to `local` instead."
    )
