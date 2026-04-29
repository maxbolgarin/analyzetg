"""SSRF guard rejects loopback / RFC1918 / link-local fetches.

Without the guard, a malicious public-looking URL could redirect to
``http://169.254.169.254/...`` (cloud metadata) or local services and
have the body summarized into the user's report. We assert:

* :func:`is_public_address` rejects every banned class.
* :func:`safe_get` rejects banned hosts at the initial URL.
* The link enricher's ``_fetch`` returns ``None`` when handed a banned
  URL (instead of falling through to fetch).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from unread.enrich.link import _fetch
from unread.util.safe_fetch import (
    BlockedURLError,
    is_public_address,
    safe_get,
    safe_validate,
)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "localhost",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # AWS / GCP / Azure metadata
        "169.254.0.1",
        "fc00::1",  # unique local
        "fe80::1",  # link-local
        "0.0.0.0",
        "service.local",
        "service.internal",
    ],
)
def test_private_addresses_rejected(host):
    assert is_public_address(host) is False


@pytest.mark.parametrize(
    "host",
    [
        "1.1.1.1",
        "8.8.8.8",
    ],
)
def test_public_addresses_allowed(host):
    assert is_public_address(host) is True


def test_safe_validate_blocks_metadata_url():
    with pytest.raises(BlockedURLError):
        safe_validate("http://169.254.169.254/latest/meta-data/")


def test_safe_validate_blocks_loopback_url():
    with pytest.raises(BlockedURLError):
        safe_validate("http://127.0.0.1:8080/admin")


def test_safe_validate_rejects_non_http():
    with pytest.raises(BlockedURLError):
        safe_validate("file:///etc/passwd")
    with pytest.raises(BlockedURLError):
        safe_validate("ftp://example.com/")


def test_safe_validate_rejects_no_host():
    with pytest.raises(BlockedURLError):
        safe_validate("http:///path")


@pytest.mark.asyncio
async def test_safe_get_rejects_initial_private_url():
    with pytest.raises(BlockedURLError):
        await safe_get("http://127.0.0.1:8080/", timeout_sec=1)


@pytest.mark.asyncio
async def test_link_enricher_fetch_returns_none_for_blocked():
    # _fetch swallows BlockedURLError and logs at info level. We assert
    # it returns None rather than reaching out to httpx.
    with patch("unread.util.safe_fetch.safe_get") as mock_get:
        # Simulate the blocked-URL path
        async def boom(*args, **kwargs):
            raise BlockedURLError("nope")

        mock_get.side_effect = boom
        result = await _fetch("http://example.com/", timeout_sec=5)
    assert result is None
