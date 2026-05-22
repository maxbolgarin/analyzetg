"""Exporters for unread messages: markdown / jsonl / csv."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from unread.analyzer.formatter import format_messages
from unread.i18n import t as _i18n_t
from unread.models import Message

# CSV "formula injection" defense (OWASP). Excel / LibreOffice / Numbers
# evaluate any cell whose first character is one of these as a formula.
# A Telegram message starting with `=cmd|'/c calc'!A0` would open calc.exe
# when the exported CSV is opened in Excel. Prefix such cells with a
# single quote so the spreadsheet renders the literal text.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """Defang an outgoing CSV cell against spreadsheet formula injection."""
    if isinstance(value, str) and value and value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def render_md(
    msgs: list[Message],
    *,
    title: str | None,
    language: str = "en",
    chat_id: int | None = None,
    thread_id: int | None = None,
    chat_link: str | None = None,
) -> str:
    """Build the markdown string without writing anything.

    Uses `blank_line_between_messages=True` so consecutive posts get a
    visible paragraph break — both for human readers of the saved `.md`
    and for Rich's CommonMark renderer in `--output console` mode, which
    would otherwise collapse adjacent lines into a single paragraph.

    When `chat_id` / `thread_id` / `chat_link` are passed, a localized
    `Chat ID: …`, `Topic ID: …`, `Chat link: …` triple is inserted right
    after the `=== Chat: <title> ===` header so the saved dump carries
    the same metadata as analyze reports.
    """
    period: tuple[datetime | None, datetime | None] = (
        msgs[0].date if msgs else None,
        msgs[-1].date if msgs else None,
    )
    body = format_messages(
        msgs,
        period=period,
        title=title,
        language=language,
        blank_line_between_messages=True,
    )
    extra: list[str] = []
    if chat_id is not None:
        extra.append(f"{_i18n_t('chat_id_label', language)}: {chat_id}")
    if thread_id:
        extra.append(f"{_i18n_t('topic_id_label', language)}: {thread_id}")
    if chat_link:
        extra.append(f"{_i18n_t('chat_link_label', language)}: {chat_link}")
    if not extra or not body:
        return body
    lines = body.split("\n")
    if lines and lines[0].startswith("==="):
        # Inject under the chat header so reading top-to-bottom is natural.
        return "\n".join([lines[0], *extra, *lines[1:]])
    return "\n".join([*extra, body])


def export_md(
    msgs: list[Message],
    *,
    title: str | None,
    output: Path,
    language: str = "en",
    chat_id: int | None = None,
    thread_id: int | None = None,
    chat_link: str | None = None,
) -> None:
    rendered = render_md(
        msgs,
        title=title,
        language=language,
        chat_id=chat_id,
        thread_id=thread_id,
        chat_link=chat_link,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    from unread.util.fsmode import tighten

    tighten(output)


def export_jsonl(msgs: list[Message], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    from unread.util.fsmode import tighten as _tighten

    with output.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(
                json.dumps(
                    {
                        "chat_id": m.chat_id,
                        "msg_id": m.msg_id,
                        "thread_id": m.thread_id,
                        "date": m.date.isoformat(),
                        "sender_id": m.sender_id,
                        "sender_name": m.sender_name,
                        "text": m.text,
                        "reply_to": m.reply_to,
                        "forward_from": m.forward_from,
                        "media_type": m.media_type,
                        "media_doc_id": m.media_doc_id,
                        "media_duration": m.media_duration,
                        "transcript": m.transcript,
                        # Enrichment fields: always present (null when
                        # that kind of enrichment didn't run or applied).
                        # Keeps the JSONL schema stable across runs with
                        # different --enrich sets.
                        "image_description": m.image_description,
                        "extracted_text": m.extracted_text,
                        "link_summaries": (
                            [[url, summary] for url, summary in m.link_summaries]
                            if m.link_summaries
                            else None
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    _tighten(output)


def export_csv(msgs: list[Message], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    from unread.util.fsmode import tighten as _tighten

    with output.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "chat_id",
                "msg_id",
                "thread_id",
                "date",
                "sender_id",
                "sender_name",
                "text",
                "reply_to",
                "forward_from",
                "media_type",
                "media_doc_id",
                "media_duration",
                "transcript",
                "image_description",
                "extracted_text",
                "link_summaries",
            ]
        )
        for m in msgs:
            # CSV can't carry structured lists; flatten link_summaries to
            # `"url1: summary1; url2: summary2"`. Newlines in summaries
            # stay as-is — Python's csv handles quoting automatically.
            links_flat = (
                "; ".join(f"{url}: {summary}" for url, summary in m.link_summaries)
                if m.link_summaries
                else ""
            )
            w.writerow(
                [
                    m.chat_id,
                    m.msg_id,
                    m.thread_id,
                    m.date.isoformat(),
                    m.sender_id,
                    _csv_safe(m.sender_name),
                    _csv_safe(m.text),
                    m.reply_to,
                    _csv_safe(m.forward_from),
                    m.media_type,
                    m.media_doc_id,
                    m.media_duration,
                    _csv_safe(m.transcript),
                    _csv_safe(m.image_description),
                    _csv_safe(m.extracted_text),
                    _csv_safe(links_flat),
                ]
            )
    _tighten(output)
