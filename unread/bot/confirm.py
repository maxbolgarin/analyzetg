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

# Actions for callback-data encoding. Telegram caps callback data at
# 64 bytes — well within reach even with 5-char action names.
#   R     = run single (used by single-item burst panel)
#   A     = run all separately (one report per burst item)
#   M     = run merged / combined (concat text → single report)
#   T_ONE = TG link: analyze just this one message
#   T_FRM = TG link: analyze from this message (cmd_analyze --from-msg)
#   T_DAY = TG link: last 1 day
#   T_WK  = TG link: last 7 days
#   T_MO  = TG link: last 30 days
_ACTIONS = frozenset(
    {
        "R",
        "A",
        "M",
        "T_ONE",
        "T_FRM",
        "T_DAY",
        "T_WK",
        "T_MO",
        # Forward-picker actions (single forwarded msg from a channel):
        "F_FULL",  # analyze the forwarded msg as-is (image + caption / image only / text)
        "F_TXT",  # analyze only the caption / inner text — skip vision
        "F_FROM",  # open the source channel and analyze from this msg onward
        "F_DAY",  # analyze the SOURCE CHANNEL — last 1 day
        "F_WK",  # — last 7 days
        "F_MO",  # — last 30 days
    }
)

# Action → `RunOptions.tg_window` value the callback handler should
# stamp before kicking off `_run_batch_separately`. None for the
# generic Run/A/M actions; only the T_* actions touch tg_window.
_TG_WINDOW_BY_ACTION: dict[str, str] = {
    "T_ONE": "msg",
    "T_FRM": "from_msg",
    "T_DAY": "1d",
    "T_WK": "7d",
    "T_MO": "30d",
}


def tg_window_for_action(action: str) -> str | None:
    """Public lookup the callback handler uses to translate a tap → window."""
    return _TG_WINDOW_BY_ACTION.get(action)


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
    YouTube uses `youtube_source`; TG uses the `enrich_*` flags plus
    `tg_window`. File and URL ignore all of them today.

    `tg_window` is set by the TG-link choice panel — one of
    `"msg" | "from_msg" | "1d" | "7d" | "30d"`. `tg.execute` reads it
    to override the default lookback (which today uses
    `s.sync.default_lookback_days`). When None, the legacy default
    applies.
    """

    youtube_source: str | None = None
    enrich_image: bool = False
    enrich_doc: bool = False
    enrich_link: bool = False
    enrich_video: bool = False
    tg_window: str | None = None


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


def build_forward_choice_panel(
    *,
    payload: dict,
    panel_msg_id: int,
) -> tuple[str, list[list[Any]]]:
    """Picker shown when a single forwarded-from-channel message arrives.

    Layout depends on what the forward carries:
      * media + caption  → [Full] [Caption only]   + [Channel · day/week/month]
      * media, no caption → [This image]            + [Channel · day/week/month]
      * text only         → [This message]          + [Channel · day/week/month]

    Caller must have detected `payload["fwd_channel_id"]` before
    calling — that's what makes the channel-window options meaningful
    (otherwise there's no source channel to pull more from).
    """
    title = payload.get("fwd_title") or "channel"
    text = f"↩ **Forwarded from {title}**\nWhat to analyze?"

    rows: list[list[Any]] = []
    has_media = payload.get("source") == "media"
    has_caption = bool(payload.get("caption"))
    has_text = payload.get("source") == "text"
    has_fwd_msg_id = bool(payload.get("fwd_msg_id"))

    # Row 1 — analyze the forwarded message itself.
    if has_media and has_caption:
        rows.append(
            [
                Button.inline("🖼 Image + caption", encode_callback("F_FULL", panel_msg_id)),
                Button.inline("📝 Caption only", encode_callback("F_TXT", panel_msg_id)),
            ]
        )
    elif has_media:
        rows.append([Button.inline("🖼 This media", encode_callback("F_FULL", panel_msg_id))])
    elif has_text:
        rows.append([Button.inline("📝 This message", encode_callback("F_FULL", panel_msg_id))])

    # Row 2 — open the source channel from this anchor message. Only
    # shown when Telegram gave us the channel-post id; otherwise the
    # bot has no anchor to walk forward from.
    if has_fwd_msg_id:
        rows.append([Button.inline("📜 From this msg in channel", encode_callback("F_FROM", panel_msg_id))])

    # Row 3 — time-window picks on the source channel.
    rows.append(
        [
            Button.inline("💬 Channel · day", encode_callback("F_DAY", panel_msg_id)),
            Button.inline("💬 Channel · week", encode_callback("F_WK", panel_msg_id)),
            Button.inline("💬 Channel · month", encode_callback("F_MO", panel_msg_id)),
        ]
    )
    return text, rows


def build_tg_choice_panel(
    *,
    url: str,
    msg_id: str | None,
    panel_msg_id: int,
) -> tuple[str, list[list[Any]]]:
    """Picker shown when a single TG link arrives in a burst.

    A private-channel `t.me/c/<id>/<msg>` link is often the only handle
    the user has on the channel — the msg id is just an incidental
    locator. This panel lets them pick what "many messages" means
    instead of defaulting to "just this message" (today's behavior).

    When the URL has no msg id (bare `@username` / `t.me/<chan>`), the
    "this message" and "from this message" options are hidden — only
    the time-window choices apply.
    """
    text = f"💬 **Telegram link**: {url}\nHow much to analyze?"
    rows: list[list[Any]] = []
    if msg_id is not None:
        rows.append(
            [
                Button.inline("📌 Just this msg", encode_callback("T_ONE", panel_msg_id)),
                Button.inline("📜 From this msg", encode_callback("T_FRM", panel_msg_id)),
            ]
        )
    rows.append(
        [
            Button.inline("📅 Last day", encode_callback("T_DAY", panel_msg_id)),
            Button.inline("📅 Last week", encode_callback("T_WK", panel_msg_id)),
            Button.inline("📅 Last month", encode_callback("T_MO", panel_msg_id)),
        ]
    )
    return text, rows


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
