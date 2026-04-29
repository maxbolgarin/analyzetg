"""HTTP redirect loops surface cleanly, not as raw HTTPError.

httpx caps redirect chains at 20 by default and raises
`httpx.TooManyRedirects` (a subclass of `httpx.HTTPError`). Two paths
need to handle this distinctly so users can grep for `redirect_loop`:

  - `unread/website/content.py:_http_get` — raises a typed
    `WebsiteFetchError` whose message names the loop, not just the
    generic "Fetch failed".
  - `unread/enrich/link.py:_fetch` — logs `enrich.link.redirect_loop`
    and returns None so the rest of the enrich phase keeps going.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.mark.asyncio
async def test_website_fetch_raises_typed_error_on_redirect_loop() -> None:
    """`_http_get` → WebsiteFetchError mentioning a redirect loop."""
    from unread.website.content import WebsiteFetchError, _http_get

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False
    fake_client.get = AsyncMock(side_effect=httpx.TooManyRedirects("21 redirects"))

    with (
        patch("unread.website.content.httpx.AsyncClient", return_value=fake_client),
        pytest.raises(WebsiteFetchError, match="redirect"),
    ):
        await _http_get(
            "https://example.com/loop",
            timeout_sec=5,
            user_agent="ua",
            max_bytes=1_000_000,
        )


@pytest.mark.asyncio
async def test_link_enricher_returns_none_on_redirect_loop(caplog) -> None:
    """`_fetch` → returns None and logs the typed `redirect_loop` key."""
    import logging

    from unread.enrich.link import _fetch

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False
    fake_client.get = AsyncMock(side_effect=httpx.TooManyRedirects("21 redirects"))

    with (
        patch("unread.enrich.link.httpx.AsyncClient", return_value=fake_client),
        caplog.at_level(logging.DEBUG, logger="unread.enrich.link"),
    ):
        result = await _fetch("https://example.com/loop", timeout_sec=5)

    assert result is None
