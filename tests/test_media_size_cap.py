"""File-size cap on `download_message` (pre-prod HIGH).

Without a cap, a 4 GB Telegram video silently fills the user's disk
and `_existing_for_msg` then ignores the partial. The cap lives in
``download_message`` itself so every call site (the `dump` path AND
the enrichment paths) gets it for free.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unread.media.download import (
    MediaTooLarge,
    download_message,
    media_size_bytes,
)


def _msg_with_doc_size(size_bytes: int):
    """Build a fake Telethon message exposing a doc.size attribute."""
    doc = SimpleNamespace(size=size_bytes)
    return SimpleNamespace(id=42, media=SimpleNamespace(), document=doc)


def _msg_with_photo_sizes(largest_bytes: int):
    photo = SimpleNamespace(sizes=[SimpleNamespace(size=largest_bytes)])
    return SimpleNamespace(id=43, media=SimpleNamespace(), photo=photo)


def test_media_size_bytes_reads_document_size():
    msg = _msg_with_doc_size(1024 * 1024 * 100)  # 100 MB
    assert media_size_bytes(msg) == 1024 * 1024 * 100


def test_media_size_bytes_reads_largest_photo_variant():
    msg = _msg_with_photo_sizes(2_000_000)
    assert media_size_bytes(msg) == 2_000_000


def test_media_size_bytes_returns_zero_for_unknown_shape():
    msg = SimpleNamespace(id=44, media=None)
    assert media_size_bytes(msg) == 0


@pytest.mark.asyncio
async def test_download_message_refuses_oversized_media(tmp_path: Path):
    """A 4 GB document is rejected with MediaTooLarge — no download attempted,
    no .part file created."""
    msg = _msg_with_doc_size(4 * 1024 * 1024 * 1024)  # 4 GB

    download_called = False

    class FakeClient:
        async def download_media(self, _msg, file: str) -> str:  # pragma: no cover
            nonlocal download_called
            download_called = True
            Path(file).write_bytes(b"x")
            return file

    out = tmp_path / "video.mp4"
    with pytest.raises(MediaTooLarge):
        await download_message(FakeClient(), msg, out, max_bytes=500 * 1024 * 1024)

    assert not download_called, "download_media must NOT be invoked for oversize media"
    # No .part file left behind
    assert not (out.with_suffix(out.suffix + ".part")).exists()
    assert not out.exists()


@pytest.mark.asyncio
async def test_download_message_passes_through_under_cap(tmp_path: Path):
    """A small file is downloaded normally and atomic-renamed."""
    msg = _msg_with_doc_size(10 * 1024)  # 10 KB

    class FakeClient:
        async def download_media(self, _msg, file: str) -> str:
            Path(file).write_bytes(b"hello-world")
            return file

    out = tmp_path / "doc.pdf"
    result = await download_message(FakeClient(), msg, out, max_bytes=1 * 1024 * 1024)
    assert result == out
    assert out.read_bytes() == b"hello-world"
    # No leftover .part
    assert not (out.with_suffix(out.suffix + ".part")).exists()


@pytest.mark.asyncio
async def test_download_message_zero_cap_disables_check(tmp_path: Path):
    """max_bytes=0 means "no cap" — even a 4 GB file passes the size guard
    (it would still fail downstream from the actual download)."""
    msg = _msg_with_doc_size(4 * 1024 * 1024 * 1024)

    class FakeClient:
        async def download_media(self, _msg, file: str) -> str:
            Path(file).write_bytes(b"\0" * 16)
            return file

    out = tmp_path / "huge.bin"
    result = await download_message(FakeClient(), msg, out, max_bytes=0)
    assert result == out


@pytest.mark.asyncio
async def test_download_message_unknown_size_passes_check(tmp_path: Path):
    """When media_size_bytes() can't read the size, the cap is skipped —
    we'd rather download a small file than refuse all unsized payloads."""
    msg = SimpleNamespace(id=99, media=None)

    class FakeClient:
        async def download_media(self, _msg, file: str) -> str:
            Path(file).write_bytes(b"ok")
            return file

    out = tmp_path / "unknown.bin"
    result = await download_message(FakeClient(), msg, out, max_bytes=100)
    assert result == out
    assert out.read_bytes() == b"ok"
