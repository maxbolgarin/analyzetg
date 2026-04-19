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


def _body(m: Message) -> str:
    body = (m.text or "").strip()
    if body:
        return body
    if m.transcript:
        return m.transcript.strip()
    if m.media_type:
        return f"[{m.media_type} без транскрипта]"
    return ""


def _dup_suffix(m: Message) -> str:
    return f" [×{m.duplicates + 1}]" if m.duplicates else ""


def _media_tag(m: Message) -> str:
    if m.media_type in {"voice", "videonote", "video"} and m.transcript:
        dur = _mmss(m.media_duration or 0)
        return f" [{m.media_type} {dur}]"
    if m.media_type in {"photo"}:
        return " [photo]"
    return ""


def _forward_tag(m: Message) -> str:
    if m.forward_from:
        return f" [fwd: {m.forward_from}]"
    return ""


def format_messages(
    msgs: list[Message],
    *,
    period: tuple[datetime | None, datetime | None] | None = None,
    title: str | None = None,
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
    lines.append("")
    for m in msgs:
        ts = m.date.strftime(date_fmt)
        who = _short_sender(m)
        reply = _reply_marker(m, idx)
        body = _body(m)
        if not body:
            continue
        lines.append(
            f"[{ts}] {who}{_forward_tag(m)}{_media_tag(m)}: {reply}{body}{_dup_suffix(m)}"
        )
    return "\n".join(lines)


def chat_header_preamble(title: str | None, period: tuple[datetime | None, datetime | None] | None) -> str:
    """Static (cacheable) portion of the prompt — appears before dynamic messages."""
    parts = []
    if title:
        parts.append(f"=== Чат: {title} ===")
    if period and (period[0] or period[1]):
        a = period[0].strftime("%Y-%m-%d") if period[0] else "…"
        b = period[1].strftime("%Y-%m-%d") if period[1] else "…"
        parts.append(f"Период: {a} — {b}")
    return "\n".join(parts)
