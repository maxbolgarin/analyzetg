"""Dense text format for messages (spec §9.5). ~30–40% cheaper than JSON.

Labels (`Period`, `Chat`, `Messages`, `Topic`, `Forum`, `[no transcript]`,
…) are looked up via `i18n.t()` so saved reports + the LLM prompt match
the user's `locale.language`. Default `language="en"` keeps direct
callers (tests, ad-hoc scripts) on a stable English label set.
"""

from __future__ import annotations

from datetime import datetime

from analyzetg.i18n import t as i18n_t
from analyzetg.models import Message


def _pick_date_format(msgs: list[Message]) -> str:
    if not msgs:
        return "%m-%d %H:%M"
    dates = [m.date for m in msgs]
    years = {d.year for d in dates}
    days = {(d.year, d.month, d.day) for d in dates}
    if len(years) > 1:
        return "%Y-%m-%d %H:%M"
    if len(days) > 1:
        return "%m-%d %H:%M"
    return "%H:%M"


def _short_sender(m: Message) -> str:
    if not m.sender_name:
        return f"id:{m.sender_id}" if m.sender_id else "unknown"
    name = m.sender_name.strip()
    if not name:
        return "unknown"
    if name.startswith("@"):
        return name
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _mmss(sec: int) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _reply_marker(m: Message, index: dict[int, Message]) -> str:
    if not m.reply_to:
        return ""
    target = index.get(m.reply_to)
    if target is None:
        return "↩? "
    return f"↩{_short_sender(target)} "


_BODY_CAP = 4000  # Hard cap per-message body length so one huge doc can't crowd a chunk.


def _body(m: Message) -> str:
    """Compose the message's analyzable body from text + enrichments.

    Order: text → image description → extracted doc text → transcript.
    Each layer is included only if present; empty after composition means the
    message has nothing analyzable and the caller drops it.
    """
    parts: list[str] = []
    text = (m.text or "").strip()
    if text:
        parts.append(text)
    if m.image_description:
        parts.append(f"[image: {m.image_description.strip()}]")
    if m.extracted_text:
        parts.append(f"[doc: {m.extracted_text.strip()}]")
    if m.transcript:
        parts.append(m.transcript.strip())
    if parts:
        combined = "\n".join(parts)
        if len(combined) > _BODY_CAP:
            combined = combined[:_BODY_CAP] + "…"
        return combined
    if m.media_type:
        # `_body` is called from `_emit_msg_line` which doesn't carry the
        # active language. Resolve from settings — cheap, cached.
        return f"[{m.media_type} {i18n_t('no_transcript')}]"
    return ""


def _link_summary_block(m: Message) -> str:
    if not m.link_summaries:
        return ""
    lines = [f"  ↳ {url}: {summary.strip()}" for url, summary in m.link_summaries]
    return "\n" + "\n".join(lines)


def _dup_suffix(m: Message) -> str:
    return f" [×{m.duplicates + 1}]" if m.duplicates else ""


def _media_tag(m: Message) -> str:
    if m.media_type in {"voice", "videonote", "video"} and m.transcript:
        dur = _mmss(m.media_duration or 0)
        return f" [{m.media_type} {dur}]"
    # For photos: only emit the bare [photo] tag when no description is
    # attached. With a description, the description goes into _body() as
    # `[image: …]` and doesn't need the duplicative tag.
    if m.media_type == "photo" and not m.image_description:
        return " [photo]"
    return ""


def _forward_tag(m: Message) -> str:
    if m.forward_from:
        return f" [fwd: {m.forward_from}]"
    return ""


def _reactions_tag(m: Message) -> str:
    """Render reactions as `[reactions: 👍×3 ❤×1 (+2 custom)]`.

    Sorted by count desc so the strongest signal comes first. Custom emoji
    counts are summed under `+N custom` to avoid dumping opaque ids into the
    prompt. Skip entirely when every reaction has count 1 AND total ≤ 1 —
    a single person reacting is noise, not signal.
    """
    if not m.reactions:
        return ""
    named: list[tuple[str, int]] = []
    custom_total = 0
    for key, count in m.reactions.items():
        if key.startswith("custom:"):
            custom_total += count
        else:
            named.append((key, count))
    named.sort(key=lambda x: (-x[1], x[0]))
    total = sum(c for _, c in named) + custom_total
    if total <= 1:
        return ""
    parts = [f"{emoji}×{count}" for emoji, count in named]
    if custom_total:
        parts.append(f"+{custom_total} custom")
    return f" [reactions: {' '.join(parts)}]"


def _topic_header(
    thread_id: int | None,
    topic_titles: dict[int, str] | None,
    *,
    language: str = "en",
) -> str | None:
    """Render the `=== Topic: X (id=Y) ===` separator for a group.

    Falls back to `Topic #<id>` when the title isn't in the map — happens
    if a topic was deleted between fetch time and analysis, or if the
    caller didn't pass the titles dict. Returns None when there's nothing
    to emit (no thread, or empty titles map).
    """
    if not topic_titles:
        return None
    label = i18n_t("topic_label", language)
    if not thread_id:
        return f"=== {label}: {i18n_t('no_topic', language)} ==="
    name = topic_titles.get(thread_id) or f"#{thread_id}"
    return f"=== {label}: {name} (id={thread_id}) ==="


def _high_impact_threshold() -> int:
    """Lazy lookup so tests / batch flows that override settings see the
    current value rather than capturing one at import time."""
    try:
        from analyzetg.config import get_settings

        return int(get_settings().analyze.high_impact_reactions)
    except Exception:
        return 3


def _high_impact_marker(m: Message) -> str:
    """Prefix `[high-impact]` for messages with `>= threshold` reactions.

    Surfaces "what people reacted to" to the LLM without breaking
    chronological order. Threshold from `[analyze].high_impact_reactions`
    in config (default 3); 0 disables the marker entirely. Soft signal —
    presets are free to ignore it.
    """
    if not m.reactions:
        return ""
    threshold = _high_impact_threshold()
    if threshold <= 0:
        return ""
    total = sum(int(v) for v in m.reactions.values() if isinstance(v, int))
    if total < threshold:
        return ""
    return "[high-impact] "


def _emit_msg_line(m: Message, idx: dict[int, Message], date_fmt: str) -> str | None:
    """Render a single message line, or None if it has no analyzable body."""
    body = _body(m)
    if not body:
        return None
    ts = m.date.strftime(date_fmt)
    who = _short_sender(m)
    reply = _reply_marker(m, idx)
    return (
        f"[{ts} #{m.msg_id}] {who}{_forward_tag(m)}{_media_tag(m)}{_reactions_tag(m)}:"
        f" {_high_impact_marker(m)}{reply}{body}{_dup_suffix(m)}{_link_summary_block(m)}"
    )


def format_messages(
    msgs: list[Message],
    *,
    period: tuple[datetime | None, datetime | None] | None = None,
    title: str | None = None,
    link_template: str | None = None,
    topic_titles: dict[int, str] | None = None,
    chat_groups: dict[int, dict] | None = None,
    language: str = "en",
) -> str:
    """Dense text format for a list of messages.

    When `topic_titles` is provided and non-empty, messages are grouped by
    `thread_id` with a `=== Топик: … ===` header before each group —
    used in all-flat forum analysis so the LLM sees coherent per-topic
    threads instead of a time-interleaved jumble of unrelated discussions.
    Groups are ordered by the date of their first message so top-to-bottom
    reading stays natural. Within each group, chronological order.

    `chat_groups` (mutually exclusive with `topic_titles`) groups messages
    by `chat_id` — used when analyzing a channel together with its linked
    discussion group (`--with-comments`). Each entry maps `chat_id ->
    {"title": str, "link_template": str | None}`; each group emits its own
    `=== Чат: … ===` header and its own `Ссылка на сообщение:` line so the
    LLM picks the right link template for citations from each chat.

    `topic_titles=None` AND `chat_groups=None` (the default) produces
    byte-identical output to the old code — crucial because every
    non-forum / per-topic / single-topic path depends on the ungrouped
    layout.
    """
    if not msgs:
        return ""
    date_fmt = _pick_date_format(msgs)
    idx = {m.msg_id: m for m in msgs}
    chat_lbl = i18n_t("chat_label", language)
    period_lbl = i18n_t("period_label", language)
    msgs_lbl = i18n_t("messages_label", language)
    msg_link_lbl = i18n_t("msg_link_label", language)
    lines: list[str] = []
    if title and not chat_groups:
        lines.append(f"=== {chat_lbl}: {title} ===")
    if period and (period[0] or period[1]):
        a = period[0].strftime("%Y-%m-%d %H:%M") if period[0] else "…"
        b = period[1].strftime("%Y-%m-%d %H:%M") if period[1] else "…"
        lines.append(f"{period_lbl}: {a} — {b}")
    lines.append(f"{msgs_lbl}: {len(msgs)}")
    if link_template and not chat_groups:
        lines.append(f"{msg_link_lbl}: {link_template}")
    lines.append("")

    if chat_groups:
        groups_by_cid: dict[int, list[Message]] = {}
        for m in msgs:
            groups_by_cid.setdefault(m.chat_id, []).append(m)
        # Stable order: channel (primary, first-encountered chat_id with
        # any messages) first, then comments. Sorting by first message's
        # date keeps reading natural across chat groups.
        ordered_cids = sorted(groups_by_cid.keys(), key=lambda c: groups_by_cid[c][0].date)
        for i, cid in enumerate(ordered_cids):
            if i > 0:
                lines.append("")
            meta = chat_groups.get(cid) or {}
            ctitle = meta.get("title") or str(cid)
            ctmpl = meta.get("link_template")
            lines.append(f"=== {chat_lbl}: {ctitle} ===")
            lines.append(f"{i18n_t('messages_in_group', language)}: {len(groups_by_cid[cid])}")
            if ctmpl:
                lines.append(f"{msg_link_lbl}: {ctmpl}")
            lines.append("")
            for m in groups_by_cid[cid]:
                line = _emit_msg_line(m, idx, date_fmt)
                if line is not None:
                    lines.append(line)
        return "\n".join(lines)

    if topic_titles:
        # Preserve input order (chronological) when building groups so
        # within-topic chronology survives the bucket pass.
        groups: dict[int, list[Message]] = {}
        for m in msgs:
            groups.setdefault(m.thread_id or 0, []).append(m)
        # Order groups by their first message's date — "which topic started
        # first in this chunk" — so readers scan top-to-bottom naturally.
        ordered_tids = sorted(groups.keys(), key=lambda tid: groups[tid][0].date)
        for i, tid in enumerate(ordered_tids):
            if i > 0:
                lines.append("")  # blank separator between topic groups
            header = _topic_header(tid, topic_titles, language=language)
            if header:
                lines.append(header)
            for m in groups[tid]:
                line = _emit_msg_line(m, idx, date_fmt)
                if line is not None:
                    lines.append(line)
        return "\n".join(lines)

    for m in msgs:
        line = _emit_msg_line(m, idx, date_fmt)
        if line is not None:
            lines.append(line)
    return "\n".join(lines)


_FORUM_TITLE_PREVIEW = 8  # Titles listed before truncation; keeps the prefix bounded.


def _forum_line(topic_titles: dict[int, str], *, language: str = "en") -> str:
    """Build the `Forum: N topic(s) — a, b, c` line for the preamble.

    Truncates at `_FORUM_TITLE_PREVIEW` names so a huge forum doesn't
    balloon the static prefix — the LLM only needs to know the forum's
    shape, not every title verbatim; per-chunk topic headers fill in the
    rest.
    """
    n = len(topic_titles)
    # Stable order: by topic_id. Not display-sorted — reading a fixed
    # order helps humans skimming the prompt.
    names = [topic_titles[k] for k in sorted(topic_titles.keys())]
    shown = names[:_FORUM_TITLE_PREVIEW]
    suffix = ""
    if n > _FORUM_TITLE_PREVIEW:
        suffix = " " + i18n_t("and_more", language).format(n=n - _FORUM_TITLE_PREVIEW)
    return f"{i18n_t('forum_label', language)}: {n} {i18n_t('topics_word', language)} — {', '.join(shown)}{suffix}"


def chat_header_preamble(
    title: str | None,
    period: tuple[datetime | None, datetime | None] | None,
    *,
    link_template: str | None = None,
    topic_titles: dict[int, str] | None = None,
    chat_groups: dict[int, dict] | None = None,
    language: str = "en",
) -> str:
    """Static (cacheable) portion of the prompt — appears before dynamic messages.

    `topic_titles`, when provided, emits one `Форум: …` line enumerating
    up to `_FORUM_TITLE_PREVIEW` topics. Sits in the static prefix so
    OpenAI's prompt cache keeps hitting across runs with the same forum
    shape; only a topic add/remove/rename invalidates it.

    `chat_groups`, when provided, emits one `Группы чатов: …` line listing
    each chat's title + link template so the LLM has a stable index of
    where each msg_id range comes from. Used by `--with-comments` runs
    (channel + linked discussion group).
    """
    chat_lbl = i18n_t("chat_label", language)
    period_lbl = i18n_t("period_label", language)
    msg_link_lbl = i18n_t("msg_link_label", language)
    link_lbl = i18n_t("link_label", language)
    parts = []
    if title and not chat_groups:
        parts.append(f"=== {chat_lbl}: {title} ===")
    if period and (period[0] or period[1]):
        a = period[0].strftime("%Y-%m-%d") if period[0] else "…"
        b = period[1].strftime("%Y-%m-%d") if period[1] else "…"
        parts.append(f"{period_lbl}: {a} — {b}")
    if topic_titles:
        parts.append(_forum_line(topic_titles, language=language))
    if chat_groups:
        for cid in sorted(chat_groups.keys()):
            meta = chat_groups[cid] or {}
            ctitle = meta.get("title") or str(cid)
            tmpl = meta.get("link_template")
            line = f"  • {ctitle}"
            if tmpl:
                line += f" — {link_lbl}: {tmpl}"
            parts.append(line)
    elif link_template:
        parts.append(f"{msg_link_lbl}: {link_template}")
    return "\n".join(parts)


def build_link_template(
    *,
    chat_username: str | None,
    chat_internal_id: int | None,
    thread_id: int | None = None,
) -> str | None:
    """Build a `https://t.me/...{msg_id}` template for the given chat.

    Returns a string with a literal `{msg_id}` placeholder the model can
    substitute, or None if no enough info to form a link.
    """
    if chat_username:
        base = f"https://t.me/{chat_username}"
    elif chat_internal_id is not None:
        base = f"https://t.me/c/{chat_internal_id}"
    else:
        return None
    if thread_id:
        return f"{base}/{thread_id}/{{msg_id}}"
    return f"{base}/{{msg_id}}"
