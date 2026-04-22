"""Exporters for analyzetg messages: markdown / jsonl / csv."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from analyzetg.analyzer.formatter import format_messages
from analyzetg.models import Message


def export_md(msgs: list[Message], *, title: str | None, output: Path) -> None:
    period: tuple[datetime | None, datetime | None] = (
        msgs[0].date if msgs else None,
        msgs[-1].date if msgs else None,
    )
    rendered = format_messages(msgs, period=period, title=title)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


def export_jsonl(msgs: list[Message], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
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
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def export_csv(msgs: list[Message], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
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
            ]
        )
        for m in msgs:
            w.writerow(
                [
                    m.chat_id,
                    m.msg_id,
                    m.thread_id,
                    m.date.isoformat(),
                    m.sender_id,
                    m.sender_name,
                    m.text,
                    m.reply_to,
                    m.forward_from,
                    m.media_type,
                    m.media_doc_id,
                    m.media_duration,
                    m.transcript,
                ]
            )
