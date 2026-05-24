"""Per-chat sticky settings + the resolution stack for a bot run.

Three sources contribute to the effective settings of any single
bot-triggered analysis:

1. **Per-run options** chosen on the inline keyboard (e.g. tapping
   `📅 Last week` sets `tg_window="7d"`).
2. **Sticky chat settings** set via slash commands (`/preset digest`,
   `/lang en`, `/enrich image,link`, `/window 30d`). Stored in
   `_chat_state[chat_id]`.
3. **Bot-wide config / global defaults** — `settings.bot.*`,
   `settings.locale.*`, `settings.enrich.*`.

Per-run wins over sticky wins over config. This module owns the
merge — call `resolve_options(...)` at the top of each handler's
`execute()` and use the returned `RunOptions` for the rest of the
call.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from unread.bot.confirm import RunOptions
from unread.config import Settings

# Known sticky-state keys on `_chat_state[chat_id]`. Kept here so
# slash-command handlers + the resolver share one source of truth.
STICKY_PRESET = "preset"
STICKY_REPORT_LANGUAGE = "report_language"
STICKY_ENRICH_EXTRAS = "enrich_extras"  # set[str] ⊆ {image, doc, link, video}
STICKY_TG_WINDOW = "tg_window"  # one of: 1d, 7d, 30d, msg, from_msg
STICKY_CONFIRM_DISABLED = "confirm_disabled"

# Names users type into the slash commands.
ENRICH_NAMES = ("image", "doc", "link", "video")
TG_WINDOW_NAMES = ("1d", "7d", "30d", "msg", "from_msg")


def resolve_options(
    *,
    chat_state: dict,
    settings: Settings,
    options: RunOptions,
) -> RunOptions:
    """Merge per-run options with sticky chat settings.

    For each field: keep the per-run value if it's set (truthy /
    non-None); otherwise fall back to the sticky chat value; otherwise
    to whatever the per-run dataclass default was. We do NOT pull
    `settings.enrich.*` here — that's `default_options`' job at panel
    build-time. The resolver is only about per-run vs sticky.
    """
    sticky_window = chat_state.get(STICKY_TG_WINDOW)
    sticky_extras: set[str] = set(chat_state.get(STICKY_ENRICH_EXTRAS) or [])

    merged = replace(
        options,
        tg_window=options.tg_window or sticky_window,
        enrich_image=options.enrich_image or ("image" in sticky_extras),
        enrich_doc=options.enrich_doc or ("doc" in sticky_extras),
        enrich_link=options.enrich_link or ("link" in sticky_extras),
        enrich_video=options.enrich_video or ("video" in sticky_extras),
    )
    return merged


def effective_report_language(chat_state: dict, settings: Settings) -> str:
    """Sticky `/lang` → config `locale.report_language` → `locale.language` → 'en'."""
    sticky = (chat_state.get(STICKY_REPORT_LANGUAGE) or "").strip()
    if sticky:
        return sticky
    return (settings.locale.report_language or settings.locale.language or "en").strip()


def effective_language(chat_state: dict, settings: Settings) -> str:
    """UI/display language. No sticky override today — uses `locale.language`."""
    return (settings.locale.language or "en").strip()


def effective_source_language(chat_state: dict, settings: Settings) -> str:
    """Source-content language hint. No sticky override — uses `locale.content_language`."""
    return (settings.locale.content_language or "").strip()


def effective_preset(chat_state: dict, settings: Settings) -> str:
    """Sticky `/preset <name>` → `bot.default_preset` → empty (kind-specific fallback)."""
    sticky = (chat_state.get(STICKY_PRESET) or "").strip()
    if sticky:
        return sticky
    return (settings.bot.default_preset or "").strip()


def smart_default_preset(kind: str) -> str:
    """Kind-appropriate fallback preset when neither sticky nor config is set.

    The `summary` preset that `cmd_analyze_file` would otherwise pick is
    tuned for chats — it produces "in the discussion" / "in the chat" style
    output, which is wrong when the user actually sent one file, one voice
    note, or one forwarded message. Override with `single_msg` for those
    one-document cases. URL / YouTube / TG-chat handlers already pick
    kind-appropriate defaults inside their own `cmd_analyze_*` so we leave
    them empty here.
    """
    return {"file": "single_msg"}.get(kind, "")


def effective_preset_for_kind(chat_state: dict, settings: Settings, kind: str) -> str:
    """Sticky `/preset` → `bot.default_preset` → smart per-kind → empty.

    Same priority as `effective_preset` but adds the smart per-kind
    layer just before the empty fallback. Use this from handlers that
    know which kind they're processing (file/url/youtube/tg).
    """
    sticky = (chat_state.get(STICKY_PRESET) or "").strip()
    if sticky:
        return sticky
    cfg = (settings.bot.default_preset or "").strip()
    if cfg:
        return cfg
    return smart_default_preset(kind)


# ---------------------------------------------------------------------------
# Slash-command parsers — shared between /enrich and /window handlers
# ---------------------------------------------------------------------------


def parse_enrich_list(arg: str) -> tuple[set[str] | None, str]:
    """Parse `/enrich <list|all|none>` argument.

    Returns `(value, status_text)`:
      * `value=None` → caller should clear sticky enrich (no extras).
      * `value=set(ENRICH_NAMES)` → /enrich all.
      * `value=set(...)` → explicit subset.
      * `value=None` + status_text starting with "Usage:" → invalid.
    """
    arg = (arg or "").strip().lower()
    if arg in ("", "none", "off", "clear"):
        return (set(), "Enrich cleared — extras are off. Default voice + videonote stay on.")
    if arg == "all":
        return (set(ENRICH_NAMES), f"Enrich set → all extras ({', '.join(ENRICH_NAMES)}).")
    requested = {p.strip() for p in arg.split(",") if p.strip()}
    bad = requested - set(ENRICH_NAMES)
    if bad:
        return (None, f"Unknown enrich kind(s): {', '.join(sorted(bad))}. Valid: {', '.join(ENRICH_NAMES)}.")
    if not requested:
        return (None, "Usage: /enrich <list|all|none>")
    return (requested, f"Enrich set → {', '.join(sorted(requested))}.")


def parse_window_value(arg: str) -> tuple[str | None, str]:
    """Parse `/window <day|week|month|msg|from_msg|none>` argument.

    Returns `(value, status_text)`. `value=""` means "clear sticky".
    `value=None` + status_text means invalid input.
    """
    arg = (arg or "").strip().lower()
    if arg in ("", "none", "off", "clear", "default"):
        return ("", "Window cleared — TG links use legacy default (last few days).")
    canonical = {
        "day": "1d",
        "1d": "1d",
        "today": "1d",
        "week": "7d",
        "7d": "7d",
        "month": "30d",
        "30d": "30d",
        "msg": "msg",
        "this": "msg",
        "from": "from_msg",
        "from_msg": "from_msg",
        "fromthis": "from_msg",
    }
    if arg not in canonical:
        valid = "day | week | month | msg | from_msg | none"
        return (None, f"Unknown window: {arg!r}. Valid: {valid}.")
    return (canonical[arg], f"Window set → {canonical[arg]}.")


def parse_lang_value(arg: str) -> tuple[str | None, str]:
    """Parse `/lang <code>` argument.

    Accepts any ISO-style code (en, ru, de, …). Empty/none clears.
    """
    arg = (arg or "").strip().lower()
    if arg in ("", "none", "off", "clear", "default"):
        return ("", "Language cleared — reports use the bot's config default.")
    if not arg.isalpha() or not (2 <= len(arg) <= 8):
        return (None, "Usage: /lang <code> (e.g. en, ru, de) or /lang none to clear.")
    return (arg, f"Report language set → {arg}.")


# ---------------------------------------------------------------------------
# /settings overview text
# ---------------------------------------------------------------------------


def render_settings_overview(chat_state: dict, settings: Settings) -> str:
    """Markdown block summarizing every sticky knob + its config fallback."""
    sticky_preset = chat_state.get(STICKY_PRESET) or ""
    sticky_lang = chat_state.get(STICKY_REPORT_LANGUAGE) or ""
    sticky_extras = sorted(chat_state.get(STICKY_ENRICH_EXTRAS) or [])
    sticky_window = chat_state.get(STICKY_TG_WINDOW) or ""
    confirm_disabled = bool(chat_state.get(STICKY_CONFIRM_DISABLED))

    cfg_preset = settings.bot.default_preset or "(kind-specific)"
    cfg_lang = settings.locale.report_language or settings.locale.language or "en"
    cfg_extras_on = [
        name for name in ("voice", "videonote", *ENRICH_NAMES) if getattr(settings.enrich, name, False)
    ]
    cfg_extras_str = ", ".join(cfg_extras_on) if cfg_extras_on else "(none)"

    def _row(label: str, sticky_val: Any, default_val: Any) -> str:
        if sticky_val:
            return f"• **{label}**: `{sticky_val}` (sticky) · default: `{default_val}`"
        return f"• **{label}**: `{default_val}` (default)"

    lines = [
        "📊 **Settings for this chat**",
        "",
        _row("Preset", sticky_preset, cfg_preset),
        _row("Report language", sticky_lang, cfg_lang),
    ]
    if sticky_extras:
        lines.append(
            f"• **Extra enrich**: `{', '.join(sticky_extras)}` (sticky) · config: `{cfg_extras_str}`"
        )
    else:
        lines.append(f"• **Extra enrich**: none (sticky) · config: `{cfg_extras_str}`")
    if sticky_window:
        lines.append(f"• **TG window**: `{sticky_window}` (sticky)")
    else:
        lines.append("• **TG window**: ask each time (default)")
    lines.append(f"• **Confirm panel**: `{'off' if confirm_disabled else 'on'}` (default: on)")
    lines.append("")
    lines.append(
        "Change with: `/preset <name>` · `/lang <code>` · `/enrich <list|all|none>` · "
        "`/window <day|week|month|msg|from_msg|none>` · `/confirm on|off`"
    )
    return "\n".join(lines)
