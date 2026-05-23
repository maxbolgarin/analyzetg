"""Per-message-shape handlers. Each module exposes a `handle(event, payload, *, app)`
coroutine that owns its own progress message + report upload."""

from __future__ import annotations
