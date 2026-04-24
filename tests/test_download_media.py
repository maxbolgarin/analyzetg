"""Filename-picking for `atg download-media`.

The command itself is a Telegram round-trip; here we pin the pure
filename-derivation helper so changes in the naming convention (how PDFs
preserve original names, how photos get `.jpg`, etc.) are explicit.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from analyzetg.media.commands import (
    _existing_for_msg,
    _safe_filename_component,
    media_filename,
)
from analyzetg.models import Message


def _msg(msg_id: int, media_type: str) -> Message:
    return Message(
        chat_id=-1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, 0),
        media_type=media_type,
        media_doc_id=msg_id * 100,
    )


def _tel_with_doc(file_name: str | None = None, mime: str = "") -> object:
    attrs = [SimpleNamespace(file_name=file_name)] if file_name else []
    doc = SimpleNamespace(attributes=attrs, mime_type=mime, size=1024)
    return SimpleNamespace(media=object(), document=doc)


def test_photo_filename():
    assert media_filename(_msg(42, "photo"), object()) == "42.jpg"


def test_voice_filename():
    assert media_filename(_msg(42, "voice"), object()) == "42.ogg"


def test_video_and_videonote_filename():
    assert media_filename(_msg(42, "video"), object()) == "42.mp4"
    assert media_filename(_msg(42, "videonote"), object()) == "42.mp4"


def test_doc_preserves_original_filename():
    # Real case: user shares "report.pdf" — the downloaded file keeps
    # the human-readable name so the folder is still searchable by name,
    # but gets a msg_id prefix so distinct messages don't collide.
    tel = _tel_with_doc(file_name="report.pdf")
    assert media_filename(_msg(99, "doc"), tel) == "99_report.pdf"


def test_doc_sanitizes_path_components():
    # Telegram filenames come from arbitrary senders; reject anything
    # that could escape the destination dir.
    tel = _tel_with_doc(file_name="../../etc/passwd")
    got = media_filename(_msg(1, "doc"), tel)
    assert "/" not in got
    assert "\\" not in got
    assert ".." not in got.split("_")[-1].split(".")[0]  # no `..`-prefix leak


def test_doc_without_filename_uses_mime_heuristic():
    tel = _tel_with_doc(mime="application/pdf")
    assert media_filename(_msg(77, "doc"), tel) == "77.pdf"
    tel_zip = _tel_with_doc(mime="application/zip")
    assert media_filename(_msg(77, "doc"), tel_zip) == "77.zip"


def test_doc_fallback_to_bin_when_nothing_known():
    tel = _tel_with_doc(mime="")
    assert media_filename(_msg(5, "doc"), tel) == "5.bin"


def test_safe_filename_rejects_path_traversal():
    assert "/" not in _safe_filename_component("a/b")
    assert "\\" not in _safe_filename_component("a\\b")
    # Leading dots are stripped so hidden-file tricks don't silently win.
    assert not _safe_filename_component("..hidden").startswith(".")


def test_existing_for_msg_finds_any_extension(tmp_path: Path):
    # --overwrite=false should skip a previously-downloaded file even if
    # the extension isn't what we'd pick today (e.g. earlier run saved
    # `123.bin` because mime was unknown).
    (tmp_path / "123.bin").write_bytes(b"x")
    assert _existing_for_msg(tmp_path, 123) == tmp_path / "123.bin"

    (tmp_path / "456_original-name.pdf").write_bytes(b"x")
    assert _existing_for_msg(tmp_path, 456) == tmp_path / "456_original-name.pdf"


def test_existing_for_msg_none_when_nothing_matches(tmp_path: Path):
    (tmp_path / "unrelated.pdf").write_bytes(b"x")
    assert _existing_for_msg(tmp_path, 999) is None
