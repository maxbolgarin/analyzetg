"""WebsiteMetadata dataclass — pure data, no network code.

Populated by `website/content.py` from extractor output (trafilatura
metadata or BeautifulSoup head tags). Stored in the `website_pages`
cache row and reflected back as the synthetic header message in
`website/commands.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WebsiteMetadata:
    url: str
    normalized_url: str
    page_id: str
    domain: str
    title: str | None = None
    site_name: str | None = None
    author: str | None = None
    published: str | None = None  # ISO date when extractable
    language: str | None = None
    word_count: int = 0
