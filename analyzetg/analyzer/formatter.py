"""Dense text format for messages (spec §9.5). ~30–40% cheaper than JSON."""

from __future__ import annotations

from datetime import datetime

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
        return f"[{m.media_type} без транскрипта]"
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


def format_messages(
    msgs: list[Message],
    *,
    period: tuple[datetime | None, datetime | None] | None = None,
    title: str | None = None,
    link_template: str | None = None,
) -> str:
    if not msgs:
        return ""
    date_fmt = _pick_date_format(msgs)
    idx = {m.msg_id: m for m in msgs}
    lines: list[str] = []
    if title:
        lines.append(f"=== Чат: {title} ===")
    if period and (period[0] or period[1]):
        a = period[0].strftime("%Y-%m-%d %H:%M") if period[0] else "…"
        b = period[1].strftime("%Y-%m-%d %H:%M") if period[1] else "…"
        lines.append(f"Период: {a} — {b}")
    lines.append(f"Сообщений: {len(msgs)}")
    if link_template:
        lines.append(f"Ссылка на сообщение: {link_template}")
    lines.append("")
    for m in msgs:
        ts = m.date.strftime(date_fmt)
        who = _short_sender(m)
        reply = _reply_marker(m, idx)
        body = _body(m)
        if not body:
            continue
        lines.append(
            f"[{ts} #{m.msg_id}] {who}{_forward_tag(m)}{_media_tag(m)}{_reactions_tag(m)}:"
            f" {reply}{body}{_dup_suffix(m)}{_link_summary_block(m)}"
        )
    return "\n".join(lines)


def chat_header_preamble(
    title: str | None,
    period: tuple[datetime | None, datetime | None] | None,
    *,
    link_template: str | None = None,
) -> str:
    """Static (cacheable) portion of the prompt — appears before dynamic messages."""
    parts = []
    if title:
        parts.append(f"=== Чат: {title} ===")
    if period and (period[0] or period[1]):
        a = period[0].strftime("%Y-%m-%d") if period[0] else "…"
        b = period[1].strftime("%Y-%m-%d") if period[1] else "…"
        parts.append(f"Период: {a} — {b}")
    if link_template:
        parts.append(f"Ссылка на сообщение: {link_template}")
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
