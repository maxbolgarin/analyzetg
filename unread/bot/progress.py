"""Bot progress-edit helper.

Every status edit goes through `edit_progress` so:

1. The inline keyboard is *always* cleared (`buttons=None`). Otherwise
   Telegram's MTProto edit can leave stale buttons attached to a
   message whose new text says "⏳ Pulling messages…" — tapping them
   then does nothing useful but confuses the user.
2. `MESSAGE_NOT_MODIFIED` and transient network errors don't tear
   down the request. Status updates are best-effort by definition.

Use everywhere instead of bare `await msg.edit(text)`.
"""

from __future__ import annotations

import contextlib
from typing import Any


async def edit_progress(msg: Any, text: str) -> None:
    """Edit `msg` to `text` with buttons cleared. Silently no-op on failure."""
    if msg is None:
        return
    with contextlib.suppress(Exception):
        await msg.edit(text, buttons=None)
