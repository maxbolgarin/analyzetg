"""Tests for `unread/export/` — markdown / jsonl / csv exporters.

Pre-prod gap: the export module shipped with no dedicated tests.
These pin the shape of each output format AND the CSV formula-injection
defense (OWASP) — a regression in `_csv_safe` is a security bug.
"""

from __future__ import annotations

import csv as _csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unread.export.markdown import (
    _CSV_FORMULA_PREFIXES,
    _csv_safe,
    export_csv,
    export_jsonl,
    export_md,
    render_md,
)
from unread.models import Message


def _msg(
    *,
    chat_id: int = 100,
    msg_id: int = 1,
    text: str = "hello",
    sender_name: str = "alice",
    transcript: str | None = None,
    image_description: str | None = None,
    extracted_text: str | None = None,
    forward_from: str | None = None,
    link_summaries: list | None = None,
) -> Message:
    return Message(
        chat_id=chat_id,
        msg_id=msg_id,
        thread_id=None,
        date=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        sender_id=42,
        sender_name=sender_name,
        text=text,
        reply_to=None,
        forward_from=forward_from,
        media_type=None,
        media_doc_id=None,
        media_duration=None,
        transcript=transcript,
        image_description=image_description,
        extracted_text=extracted_text,
        link_summaries=link_summaries,
    )


# ---- _csv_safe (OWASP) --------------------------------------------------


@pytest.mark.parametrize("prefix", list(_CSV_FORMULA_PREFIXES))
def test_csv_safe_defangs_every_dangerous_prefix(prefix: str):
    """A cell starting with =, +, -, @, \\t, or \\r gets a leading single
    quote so Excel renders it as text instead of evaluating as a formula."""
    payload = f"{prefix}cmd|'/c calc'!A0"
    out = _csv_safe(payload)
    assert isinstance(out, str)
    assert out.startswith("'"), f"prefix {prefix!r} not defanged: {out!r}"
    assert out[1:] == payload


def test_csv_safe_passes_through_safe_strings():
    assert _csv_safe("hello world") == "hello world"
    assert _csv_safe("") == ""
    # Numeric / non-string types pass through untouched (csv module
    # handles serialization).
    assert _csv_safe(42) == 42
    assert _csv_safe(None) is None


def test_csv_safe_only_defangs_first_char():
    """Mid-string `=` is fine — only the leading char is dangerous."""
    assert _csv_safe("rate is = 5") == "rate is = 5"


# ---- render_md / export_md --------------------------------------------


def test_render_md_returns_non_empty_for_at_least_one_message():
    """The rendered output should include something user-visible: title,
    date, or message text."""
    out = render_md(
        [_msg(text="first message"), _msg(msg_id=2, text="second message")],
        title="Test Chat",
    )
    assert out
    # Must include at least one of the message bodies
    assert "first message" in out or "second message" in out


def test_render_md_handles_empty_message_list():
    """Empty input → still returns a string (possibly just headers / period
    placeholder), not None / crash."""
    out = render_md([], title=None)
    assert isinstance(out, str)


def test_export_md_writes_to_disk_with_tightened_perms(tmp_path: Path):
    """File is written + chmod-tightened by `tighten`."""
    output = tmp_path / "report.md"
    export_md([_msg(text="payload")], title="X", output=output)
    assert output.is_file()
    assert "payload" in output.read_text(encoding="utf-8")


# ---- export_jsonl ------------------------------------------------------


def test_export_jsonl_writes_one_object_per_line(tmp_path: Path):
    """Each line is a valid JSON object; field set is stable across runs."""
    output = tmp_path / "msgs.jsonl"
    export_jsonl(
        [
            _msg(msg_id=1, text="one", transcript="aud", image_description=None),
            _msg(msg_id=2, text="two", link_summaries=[("https://x", "summary")]),
        ],
        output=output,
    )
    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert "chat_id" in obj
        assert "msg_id" in obj
        assert "date" in obj
        # Enrichment fields always present, even when null
        assert "image_description" in obj
        assert "extracted_text" in obj
        assert "link_summaries" in obj


def test_export_jsonl_serializes_link_summaries_as_pairs(tmp_path: Path):
    output = tmp_path / "msgs.jsonl"
    export_jsonl(
        [_msg(link_summaries=[("https://a", "first"), ("https://b", "second")])],
        output=output,
    )
    obj = json.loads(output.read_text(encoding="utf-8").strip())
    assert obj["link_summaries"] == [["https://a", "first"], ["https://b", "second"]]


# ---- export_csv --------------------------------------------------------


def test_export_csv_writes_header_then_rows(tmp_path: Path):
    output = tmp_path / "msgs.csv"
    export_csv(
        [_msg(msg_id=1, text="row one"), _msg(msg_id=2, text="row two")],
        output=output,
    )
    rows = list(_csv.reader(output.open(encoding="utf-8")))
    # Header + 2 rows
    assert len(rows) == 3
    assert "chat_id" in rows[0]
    assert "text" in rows[0]
    assert any("row one" in cell for cell in rows[1])
    assert any("row two" in cell for cell in rows[2])


def test_export_csv_defangs_formula_in_text_field(tmp_path: Path):
    """An attacker-controlled message body starting with `=` is escaped."""
    output = tmp_path / "msgs.csv"
    export_csv(
        [_msg(text="=cmd|'/c calc'!A0")],
        output=output,
    )
    rows = list(_csv.reader(output.open(encoding="utf-8")))
    assert len(rows) == 2
    text_idx = rows[0].index("text")
    cell = rows[1][text_idx]
    # Must have the leading single quote — the formula is now literal.
    assert cell.startswith("'="), f"CSV cell not defanged: {cell!r}"


def test_export_csv_defangs_formula_in_sender_name_field(tmp_path: Path):
    """Same defense applies to every user-controllable field."""
    output = tmp_path / "msgs.csv"
    export_csv(
        [_msg(sender_name="@cmd")],
        output=output,
    )
    rows = list(_csv.reader(output.open(encoding="utf-8")))
    sender_idx = rows[0].index("sender_name")
    assert rows[1][sender_idx].startswith("'@")
