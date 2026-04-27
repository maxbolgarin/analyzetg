"""Shared types for the enrichment subsystem.

`EnrichOpts` is the per-run contract passed from CLI/interactive/preset/config
down to the orchestrator. `EnrichResult` and `EnrichStats` describe what an
enricher returns and what the orchestrator aggregates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EnrichOpts:
    """Per-run enrichment settings, merged from CLI > interactive > preset > config.

    Bools toggle the kind; the model fields override config defaults; the
    *_cap fields let callers stop a runaway run (e.g. a chat with 500 photos).
    `concurrency` bounds parallel API calls per run.
    """

    voice: bool = False
    videonote: bool = False
    video: bool = False
    image: bool = False
    doc: bool = False
    link: bool = False

    vision_model: str | None = None
    doc_model: str | None = None
    link_model: str | None = None
    audio_model: str | None = None

    max_images_per_run: int = 50
    max_link_fetches_per_run: int = 50
    max_doc_bytes: int = 5_000_000
    max_doc_chars: int = 20_000
    link_fetch_timeout_sec: int = 10
    skip_link_domains: list[str] = field(default_factory=list)
    concurrency: int = 3

    def any_enabled(self) -> bool:
        return any((self.voice, self.videonote, self.video, self.image, self.doc, self.link))

    def kinds_enabled(self) -> list[str]:
        """Sorted list of enabled kinds, for stable cache-key hashing."""
        out = []
        if self.voice:
            out.append("voice")
        if self.videonote:
            out.append("videonote")
        if self.video:
            out.append("video")
        if self.image:
            out.append("image")
        if self.doc:
            out.append("doc")
        if self.link:
            out.append("link")
        return out


@dataclass(slots=True)
class EnrichResult:
    """One enricher's verdict for one message (or one URL)."""

    kind: str  # transcript | image_description | doc_extract | link_summary | video_description
    content: str  # the text to splice into the message
    cost_usd: float = 0.0
    model: str | None = None
    cache_hit: bool = False


@dataclass(slots=True)
class EnrichStats:
    """Aggregate counts and cost across one run's enrichment pass.

    Shown to the user ("Enriched: 12 voice, 3 image — $0.28") and logged.
    """

    counts: dict[str, int] = field(default_factory=dict)  # kind → done count
    cache_hits: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0

    def record(self, kind: str, result: EnrichResult) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if result.cache_hit:
            self.cache_hits[kind] = self.cache_hits.get(kind, 0) + 1
        self.total_cost_usd += result.cost_usd or 0.0

    def record_error(self, kind: str) -> None:
        self.errors[kind] = self.errors.get(kind, 0) + 1

    def record_skip(self, kind: str) -> None:
        self.skipped[kind] = self.skipped.get(kind, 0) + 1

    def summary(self) -> str:
        if not self.counts and not self.skipped:
            return ""
        pieces = []
        for kind in sorted(set(self.counts) | set(self.skipped) | set(self.errors)):
            n = self.counts.get(kind, 0)
            hits = self.cache_hits.get(kind, 0)
            skipped = self.skipped.get(kind, 0)
            errs = self.errors.get(kind, 0)
            part = f"{kind}: {n}"
            if hits:
                part += f" ({hits} cached)"
            if skipped:
                part += f", {skipped} skipped"
            if errs:
                part += f", {errs} failed"
            pieces.append(part)
        tail = f" — ${self.total_cost_usd:.4f}" if self.total_cost_usd else ""
        return "Enriched: " + "; ".join(pieces) + tail
