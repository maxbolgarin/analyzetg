"""CSV formula-injection defense.

Excel / LibreOffice / Numbers evaluate any cell whose first character
is `=`, `+`, `-`, `@`, `\\t`, or `\\r` as a formula. A Telegram message
starting with `=cmd|'/c calc'!A0` opens calc.exe when the exported
CSV is opened in Excel. Pre-prod review #6: prefix such cells with a
single quote.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from unread.export.markdown import _csv_safe, export_csv
from unread.models import Message


def test_csv_safe_prefixes_equals():
    assert _csv_safe("=cmd") == "'=cmd"


def test_csv_safe_prefixes_plus_minus_at():
    assert _csv_safe("+1+1") == "'+1+1"
    assert _csv_safe("-2") == "'-2"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"


def test_csv_safe_prefixes_tab_and_cr():
    assert _csv_safe("\tinjection") == "'\tinjection"
    assert _csv_safe("\rfoo") == "'\rfoo"


def test_csv_safe_passes_through_normal_text():
    assert _csv_safe("hello") == "hello"
    assert _csv_safe("hello = world") == "hello = world"
    assert _csv_safe("") == ""


def test_csv_safe_passes_non_strings_unchanged():
    assert _csv_safe(42) == 42
    assert _csv_safe(None) is None


def test_export_csv_defangs_attacker_controlled_text(tmp_path: Path):
    """End-to-end: an exported CSV row whose `text` starts with `=`
    must have the cell prefixed with a single quote so Excel doesn't
    evaluate it as a formula."""
    msg = Message(
        chat_id=-1,
        msg_id=1,
        date=datetime(2026, 4, 24, 12, 0),
        sender_id=99,
        sender_name="Mallory",
        text="=cmd|'/c calc'!A0",
        transcript=None,
    )
    out = tmp_path / "dump.csv"
    export_csv([msg], out)
    content = out.read_text(encoding="utf-8")
    # The attacker payload must be defanged — the dangerous cell must
    # appear as quoted text (`'=cmd...`), not as a bare formula. The
    # literal substring still appears in the file (we kept the text
    # for the user to read), but is now prefixed by a single quote
    # which Excel renders as literal text.
    rows = content.splitlines()
    data_row = rows[1]
    assert "'=cmd" in data_row, "expected single-quote-prefixed payload"
    # And the unquoted form must NOT appear as the start of any cell.
    cells = data_row.split(",")
    for cell in cells:
        assert not cell.lstrip('"').startswith("=cmd"), f"cell starts with bare formula prefix: {cell!r}"
