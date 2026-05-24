"""Pre-analyze confirm panel — inline keyboard the bot replies with.

Sits between `dispatcher.classify` and the kind-specific handler:
the handler builds `RunOptions` from settings, sends a one-tap panel,
and records a `PendingRun` keyed by the panel message ID. Taps come
back via `events.CallbackQuery` in `app.py:_handle_callback`, which
mutates options, re-renders the panel, or runs / cancels.

Everything in this module is pure logic — no Telethon I/O, no `await`.
Construction returns `(text, buttons)` tuples the handler passes to
`event.reply(..., buttons=buttons)` or `event.edit(..., buttons=...)`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from telethon import Button

from unread.config import Settings

# Actions for callback-data encoding. Single char keeps the payload
# comfortably under Telegram's 64-byte cap.
#   R = run single (legacy — pre-burst path)
#   A = run all separately (one report per burst item)
#   M = run merged / combined (concat extracted text, single report)
_ACTIONS = frozenset({"R", "A", "M"})

# Kind → preset name that `cmd_analyze*` would fall back to if the
# caller passes `preset=None`. Mirrors the `preset or "<name>"` lines
# in `unread/files/commands.py`, `unread/website/commands.py`,
# `unread/youtube/commands.py`, and `unread/analyzer/commands.py`.
# The confirm panel uses this so a "no sticky preset" chat still
# shows a concrete name instead of a vague placeholder.
_DEFAULT_PRESET_BY_KIND = {
    "file": "summary",
    "url": "website",
    "youtube": "video",
    "tg": "summary",
}


def default_preset_for_kind(kind: str) -> str:
    """Static fallback preset name `cmd_analyze*` uses when none is passed."""
    return _DEFAULT_PRESET_BY_KIND.get(kind, "summary")


@dataclass
class RunOptions:
    """Per-run knobs the confirm panel exposes.

    Only fields meaningful for the kind in question are populated:
    YouTube uses `youtube_source`; TG uses the `enrich_*` flags. File
    and URL ignore all of them today.
    """

    youtube_source: str | None = None
    enrich_image: bool = False
    enrich_doc: bool = False
    enrich_link: bool = False
    enrich_video: bool = False


@dataclass
class PendingRun:
    """One open confirm panel awaiting a tap.

    Stored in `app._chat_state[chat_id]["pending_runs"][panel_msg_id]`.
    `created_at` is epoch seconds so `prune_pending_runs` can drop
    stale entries (default TTL 1h) without leaking memory across a
    long-lived bot process. `event` is the original `NewMessage.Event`
    the user sent — kept so `execute(...)` can still post replies and
    upload the report against the original message after a callback
    tap (the CallbackQuery event itself replies to the panel message,
    which would orphan the report from the user's question).
    """

    kind: str
    payload: dict
    options: RunOptions
    event: Any = None
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_options(kind: str, settings: Settings) -> RunOptions:
    """Build the initial `RunOptions` shown when the panel first appears.

    YouTube starts on `auto` (captions-then-Whisper). TG enrich
    toggles mirror `settings.enrich.*` so the panel matches what would
    have happened with no confirm at all. File and URL produce a
    zeroed `RunOptions` since they expose no knobs.
    """
    if kind == "youtube":
        return RunOptions(youtube_source="auto")
    if kind == "tg":
        e = settings.enrich
        return RunOptions(
            enrich_image=bool(e.image),
            enrich_doc=bool(e.doc),
            enrich_link=bool(e.link),
            enrich_video=bool(e.video),
        )
    return RunOptions()


# ---------------------------------------------------------------------------
# Callback-data encoding
# ---------------------------------------------------------------------------


def encode_callback(action: str, panel_msg_id: int, arg: str | None = None) -> bytes:
    """Pack `(action, panel_msg_id[, arg])` into a Telegram-safe payload.

    Format: ``b"<action>:<panel_msg_id>[:<arg>]"``. The 64-byte cap is
    enforced by Telegram; with single-char actions and 6-char arg names
    it leaves room for ~50 digits of message ID — far more than needed.
    """
    if action not in _ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    if arg is None:
        return f"{action}:{panel_msg_id}".encode()
    return f"{action}:{panel_msg_id}:{arg}".encode()


def parse_callback(data: bytes) -> tuple[str, int, str | None]:
    """Inverse of `encode_callback`. Raises `ValueError` on garbage."""
    if not data:
        raise ValueError("empty callback data")
    try:
        s = data.decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"non-ascii callback data: {data!r}") from e
    parts = s.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"malformed callback data: {data!r}")
    action = parts[0]
    if action not in _ACTIONS:
        raise ValueError(f"unknown action {action!r} in {data!r}")
    try:
        msg_id = int(parts[1])
    except ValueError as e:
        raise ValueError(f"bad panel_msg_id in {data!r}") from e
    arg = parts[2] if len(parts) == 3 else None
    return (action, msg_id, arg)


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------


def build_initial_panel(
    *,
    kind: str,
    payload: dict,
    options: RunOptions,
    preset: str,
    panel_msg_id: int,
) -> tuple[str, list[list[Any]]]:
    """The confirm panel: summary lines + one [▶ Run] button.

    All per-run tuning lives in slash commands now (`/preset <name>`
    for the preset; future `/source` / `/enrich` if those become
    worth exposing). The panel exists only to gate analyze on an
    explicit tap so messages aren't accidentally summarized.
    """
    text = _initial_text(kind, payload, options, preset)
    rows: list[list[Any]] = [[Button.inline("▶ Run", encode_callback("R", panel_msg_id))]]
    return text, rows


def _initial_text(kind: str, payload: dict, options: RunOptions, preset: str) -> str:
    """Render the summary lines shown above the Run button.

    `preset` resolves to a concrete name (sticky `/preset` →
    `s.bot.default_preset` → kind-specific fallback) so the panel
    never has to print a "_(default)_" placeholder. Same for the TG
    enrich line: shows the actual enabled list (voice + videonote by
    default) instead of a "none beyond defaults" placeholder.
    """
    resolved_preset = preset or default_preset_for_kind(kind)
    preset_line = f"Preset: `{resolved_preset}`"
    if kind == "file":
        name = payload.get("name") or "file"
        sub_kind = payload.get("kind") or "file"
        return f"📄 **{sub_kind}**: `{name}`\n{preset_line}"
    if kind == "url":
        return f"🌐 **Web page**: {payload.get('url', '')}\n{preset_line}"
    if kind == "youtube":
        mode = options.youtube_source or "auto"
        return f"🎬 **YouTube**: {payload.get('url', '')}\n{preset_line}\nMode: `{mode}`"
    if kind == "tg":
        enabled = _enabled_enrich_labels(options)
        return f"💬 **Telegram**: {payload.get('url', '')}\n{preset_line}\nEnrich: {', '.join(enabled)}"
    return f"**{kind}**\n{preset_line}"


def _enabled_enrich_labels(options: RunOptions) -> list[str]:
    """voice + videonote (always-on baseline) plus any extras toggled on."""
    out: list[str] = ["voice", "videonote"]
    if options.enrich_image:
        out.append("image")
    if options.enrich_doc:
        out.append("doc")
    if options.enrich_link:
        out.append("link")
    if options.enrich_video:
        out.append("video")
    return out


def build_batch_panel(
    *,
    items: list,
    panel_msg_id: int,
) -> tuple[str, list[list[Any]]]:
    """Panel shown after a burst of messages settles.

    `items` is a list of `unread.bot.burst.BurstItem`. Lists each source
    as a bullet and offers two run modes:

      * ▶ Run separately — one analyze per item, N reports.
      * ▶ Run combined   — concatenate extracted text from every item,
        run a single analyze, one report.

    For a single-item burst the combined / separately distinction is
    moot, so we collapse to one ▶ Run button. The combined button is
    also hidden when no items in the burst are combinable today (TG
    links aren't supported yet by the merged extractor).
    """
    from unread.bot.burst import combinable_items, summary_line

    if not items:
        # Defensive — flush should never call us with an empty burst.
        return ("(no messages in burst)", [])

    if len(items) == 1:
        # Same shape as the legacy single-message panel.
        bullet = summary_line(items[0])
        text = f"{bullet}\nReady?"
        rows: list[list[Any]] = [[Button.inline("▶ Run", encode_callback("R", panel_msg_id))]]
        return text, rows

    bullets = "\n".join(f"• {summary_line(it)}" for it in items)
    text = f"📥 **{len(items)} items ready to analyze:**\n{bullets}"
    combinable = combinable_items(items)
    row: list[Any] = [
        Button.inline(
            f"▶ Run separately ({len(items)} reports)",
            encode_callback("A", panel_msg_id),
        )
    ]
    if combinable:
        # When some items aren't combinable, label tells the user how
        # many will actually merge.
        label = (
            "▶ Run combined (1 report)"
            if len(combinable) == len(items)
            else f"▶ Run combined ({len(combinable)} of {len(items)})"
        )
        row.append(Button.inline(label, encode_callback("M", panel_msg_id)))
    return text, [row]


# ---------------------------------------------------------------------------
# TTL pruning
# ---------------------------------------------------------------------------


def prune_pending_runs(chat_state: dict, *, ttl_seconds: float = 3600.0, now: float | None = None) -> None:
    """Drop `PendingRun` entries older than `ttl_seconds`.

    Called lazily from the callback handler on every tap so memory
    can't grow unbounded across a long-lived bot process. No-op when
    `pending_runs` hasn't been seeded yet.
    """
    pending = chat_state.get("pending_runs")
    if not pending:
        return
    cutoff = (time.time() if now is None else now) - ttl_seconds
    stale = [mid for mid, pr in pending.items() if pr.created_at < cutoff]
    for mid in stale:
        pending.pop(mid, None)


# ---------------------------------------------------------------------------
# Helpers exposed for handlers
# ---------------------------------------------------------------------------


def enrich_csv(options: RunOptions) -> str:
    """Comma-joined enrich kinds the user enabled, for `cmd_analyze(enrich=...)`.

    Empty string when nothing extra was toggled — callers should treat
    `""` as "fall back to settings.enrich.*", same as the CLI's
    `--enrich ""` semantics.
    """
    return ",".join(_enabled_enrich_labels(options))
