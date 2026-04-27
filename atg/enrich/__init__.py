"""Per-media-type enrichment: turn voice / video / images / documents / links
into plain text so the analyzer sees every message, not just the text ones.

Each enricher writes to `media_enrichments` (or `link_enrichments`) keyed by
content-addressable id, so the same photo forwarded across 10 chats is
described once. The orchestrator (`enrich.pipeline.enrich_messages`) is the
only public entry point most callers need; the individual enrichers are
exposed for tests.
"""

from atg.enrich.base import EnrichOpts, EnrichResult, EnrichStats

__all__ = ["EnrichOpts", "EnrichResult", "EnrichStats"]
