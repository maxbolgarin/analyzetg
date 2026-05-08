"""Custom date range counts use UTC-aware datetimes (pre-prod HIGH).

`_count_custom_range` passes the user-supplied since/until as Telethon's
`offset_date`. Naive datetimes get interpreted in the host's local TZ
by python's datetime → epoch conversions, skewing the confirm-screen
count by the host's UTC offset. The fix is at the call site
(`interactive.py` ~972) — apply `.replace(tzinfo=UTC)` before passing
into the helper.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from unread.interactive import _count_custom_range


def test_call_site_passes_utc_aware_datetimes_to_count_custom_range():
    """Pin the call-site fix in the custom-range step — the strptime
    result must have .replace(tzinfo=UTC) applied so it lands tz-aware
    by the time it reaches `_count_custom_range` / `_count_custom_range_topic`.

    The construction may live inline at the call site or in local
    variables a few lines above (e.g. when the step dispatches between
    chat-wide and topic-scoped helpers); either layout is fine as long
    as both bounds end up tz-aware before the helper sees them.
    """
    src = inspect.getsource(__import__("unread.interactive", fromlist=["interactive"]))
    # Anchor on the wizard's custom-range handler block (not the
    # `_period_to_db_filters` helper, which has a similar `period ==
    # "custom"` check). The dispatch between chat-wide and topic-scoped
    # helpers pushed the kwargs out of a tight window after the call,
    # so we anchor at the block start instead.
    idx = src.find('if period == "custom" and chat is not None')
    assert idx > 0, "couldn't find the wizard custom-range handler block"
    # Wide window: covers both since/until construction AND the call(s).
    window = src[idx : idx + 1600]
    assert "_count_custom_range" in window, "custom-range block must call the helper"
    # Both `since=` and `until=` bounds must apply tzinfo=UTC.
    assert "tzinfo=UTC" in window, f"custom-range handler must build tz-aware datetimes; got:\n{window}"
    assert window.count("tzinfo=UTC") >= 2, "both since= and until= must be tz-aware"


async def test_count_custom_range_accepts_utc_aware_inputs():
    """Helper itself doesn't crash when given tz-aware UTC datetimes —
    the offset_date kwarg accepts them."""
    captured: dict = {}

    class FakeClient:
        async def get_messages(self, _chat_id, **kwargs):
            captured.setdefault("calls", []).append(kwargs)
            # Mimic Telethon: returns a list of pseudo-messages with .id
            return [type("M", (), {"id": 100})()]

    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime(2026, 5, 1, tzinfo=UTC)
    n = await _count_custom_range(FakeClient(), chat_id=1, thread_id=None, since=since, until=until)
    # 2 calls (upper + lower), each carrying offset_date.
    assert len(captured["calls"]) == 2
    for call in captured["calls"]:
        offset = call.get("offset_date")
        if offset is not None:
            assert offset.tzinfo is UTC, "offset_date must reach Telethon as tz-aware"
    # min/max range → 1 message
    assert n == 1
