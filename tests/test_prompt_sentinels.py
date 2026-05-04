"""Untrusted-content sentinel rendering (Task 3.2 — prompt-injection mitigation).

The formatter wraps every third-party body (message text, transcripts,
image / doc excerpts, fetched link summaries) in
`<<<UNTRUSTED_CONTENT id=…>>> / <<<END_UNTRUSTED>>>` markers; the model
is told (via `presets/<lang>/_base.md`) to treat anything inside those
markers as data, never instructions. These tests pin the structural
guarantees the rest of the pipeline relies on:

  * sentinel pairs balance (open count == close count), so the model
    never sees a half-wrapped block;
  * trusted control structure (msg-id headers, timestamps, the
    `↳ url:` prefix) stays OUTSIDE sentinels — wrapping our own
    headers would let an attacker forge `<<<END_UNTRUSTED>>>` and
    escape the block;
  * `BASE_VERSION == "v7"` — bumping that constant busts every cached
    `analysis_cache` row so v6 results (generated against the un-wrapped
    prompt) get re-run with the new sentinel discipline.
"""

from __future__ import annotations

import re
from datetime import datetime

from unread.analyzer import prompts
from unread.analyzer.formatter import format_messages
from unread.models import Message


def _msg(msg_id: int, date: datetime, **kw) -> Message:
    base = {"chat_id": 1, "msg_id": msg_id, "date": date, "sender_name": "Alice"}
    base.update(kw)
    return Message(**base)


def test_sentinels_balance_across_chunk() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    msgs = [
        _msg(1, d, text="hello world"),
        _msg(2, d, text="reply", reply_to=1, sender_name="Bob"),
        _msg(
            3,
            d,
            text="see this",
            link_summaries=[("https://example.com/a", "an article about X")],
        ),
    ]
    out = format_messages(msgs, title="Test", period=(d, d))
    opens = out.count("<<<UNTRUSTED_CONTENT id=")
    closes = out.count("<<<END_UNTRUSTED>>>")
    assert opens == closes
    # Three message bodies + one link summary = four sentinel pairs.
    assert opens == 4
    # Each msg's body is wrapped with the matching id.
    assert "<<<UNTRUSTED_CONTENT id=1>>>" in out
    assert "<<<UNTRUSTED_CONTENT id=2>>>" in out
    assert "<<<UNTRUSTED_CONTENT id=3>>>" in out


def test_msg_id_header_outside_sentinels() -> None:
    """Headers must be trusted control structure; if they were inside a
    sentinel block, the model would treat msg-id markers as data and
    citations would break (and an attacker could spoof headers from
    inside a wrapped body)."""
    d = datetime(2026, 4, 19, 12, 0)
    msgs = [_msg(42, d, text="payload")]
    out = format_messages(msgs)
    # Find each `[<ts> #<id>]` header and prove it sits OUTSIDE every
    # sentinel pair on the same render.
    header_re = re.compile(r"\[\d{2}:\d{2} #(\d+)\]")
    matches = list(header_re.finditer(out))
    assert matches, "expected at least one msg header"
    for m in matches:
        # Walk forward from the header position; the next sentinel-related
        # marker must be `<<<UNTRUSTED_CONTENT id=…>>>` (an open), not
        # `<<<END_UNTRUSTED>>>` (a close). A close-before-open at this
        # position would mean the header had been swallowed into a block.
        tail = out[m.end() :]
        next_open = tail.find("<<<UNTRUSTED_CONTENT id=")
        next_close = tail.find("<<<END_UNTRUSTED>>>")
        assert next_open != -1, "expected an open sentinel after header"
        assert next_close == -1 or next_open < next_close, "msg-id header was rendered inside a sentinel pair"


def test_link_summary_url_prefix_outside_sentinels() -> None:
    """The `↳ url:` prefix is trusted (we extracted the URL ourselves);
    only the fetched summary text gets wrapped."""
    d = datetime(2026, 4, 19, 12, 0)
    msg = _msg(
        7,
        d,
        text="check this",
        link_summaries=[("https://example.com/a", "fetched body text")],
    )
    out = format_messages([msg])
    # The url prefix line must precede an open sentinel — not be inside one.
    prefix_pos = out.find("↳ https://example.com/a:")
    assert prefix_pos != -1
    # Walk backwards from the prefix; the closest sentinel-related marker
    # before it must be a CLOSE (the prior body's <<<END_UNTRUSTED>>>),
    # not an unclosed OPEN — otherwise the url itself sits inside a block.
    head = out[:prefix_pos]
    last_open = head.rfind("<<<UNTRUSTED_CONTENT id=")
    last_close = head.rfind("<<<END_UNTRUSTED>>>")
    assert last_close > last_open, "url prefix was rendered inside a sentinel pair"


def test_base_version_anchored_at_v7() -> None:
    """Cache-bust intent: bumping BASE_VERSION rekeys every analysis_cache
    row so cached v6 results (which were generated against the un-wrapped
    prompt) get re-run with the new sentinel discipline."""
    assert prompts.BASE_VERSION == "v7"
