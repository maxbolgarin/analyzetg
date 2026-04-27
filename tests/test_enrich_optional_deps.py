"""Soft-dependency behavior for the link + document enrichers.

Missing `beautifulsoup4`, `pypdf`, or `python-docx` must not crash the
analyzer. Each enricher short-circuits with a log warning (same pattern
as `FfmpegMissing` in `enrich/audio.py`) and the rest of the run
proceeds. These tests pin that behavior so a future tidy-up can't
quietly re-introduce the eager-import regression.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from unread.enrich import document as doc_mod
from unread.enrich import link as link_mod
from unread.models import Message


class _StubRepo:
    """Minimum repo surface exercised by the short-circuit path + happy-path
    extract. Lib-missing checks return None before touching put_*, but the
    plain-text extraction test reaches the cache-write, so we need no-op
    writers too.
    """

    async def get_link_enrichment(self, *_a, **_kw):
        return None

    async def get_media_enrichment(self, *_a, **_kw):
        return None

    async def put_media_enrichment(self, *_a, **_kw):
        return None

    async def put_link_enrichment(self, *_a, **_kw):
        return None


class _StubClient:
    def __init__(self, tel_msg):
        self._msg = tel_msg

    async def get_messages(self, *_a, **_kw):
        return self._msg


def _fake_doc_tel_msg(filename: str):
    """A minimal Telethon-shaped object `_ext_of` understands.

    `_ext_of` reads `tel_msg.document.attributes[*].file_name`, so a
    `SimpleNamespace` tree is enough — no telethon dep.
    """
    doc = SimpleNamespace(
        attributes=[SimpleNamespace(file_name=filename)],
        mime_type="",
        size=1024,
    )
    return SimpleNamespace(media=object(), document=doc)


def _doc_message() -> Message:
    return Message(
        chat_id=-1,
        msg_id=10,
        date=datetime(2026, 4, 24, 12, 0),
        media_type="doc",
        media_doc_id=42,
    )


# --- bs4 ---------------------------------------------------------------


def test_has_bs4_true_when_installed():
    # Happy path — CI always has beautifulsoup4, so the flag should be True.
    # Documents intent: module-level import must never raise.
    assert link_mod._HAS_BS4 is True


async def test_enrich_url_skips_when_bs4_missing(monkeypatch):
    monkeypatch.setattr(link_mod, "_HAS_BS4", False)
    result = await link_mod.enrich_url("https://example.com/foo", repo=_StubRepo())
    assert result is None


# --- pypdf -------------------------------------------------------------


def test_has_pypdf_true_when_installed():
    assert doc_mod._HAS_PYPDF is True


async def test_enrich_document_skips_pdf_when_pypdf_missing(monkeypatch):
    monkeypatch.setattr(doc_mod, "_HAS_PYPDF", False)
    client = _StubClient(_fake_doc_tel_msg("report.pdf"))
    result = await doc_mod.enrich_document(_doc_message(), client=client, repo=_StubRepo())
    assert result is None


# --- python-docx -------------------------------------------------------


def test_has_docx_true_when_installed():
    assert doc_mod._HAS_DOCX is True


async def test_enrich_document_skips_docx_when_python_docx_missing(monkeypatch):
    monkeypatch.setattr(doc_mod, "_HAS_DOCX", False)
    client = _StubClient(_fake_doc_tel_msg("memo.docx"))
    result = await doc_mod.enrich_document(_doc_message(), client=client, repo=_StubRepo())
    assert result is None


# --- Defaults: size cap must be generous enough for realistic docs ----


def test_default_doc_size_cap_accepts_typical_pdfs():
    """Regression guard: the initial 5 MB default rejected a user's 7.7 MB
    PDF in a real run. 25 MB is the right ceiling — small enough to block
    pathological uploads, large enough for everyday documents.
    """
    from unread.config import EnrichCfg

    cfg = EnrichCfg()
    assert cfg.max_doc_bytes >= 25_000_000, (
        f"max_doc_bytes={cfg.max_doc_bytes} is too conservative — "
        "a 7.7 MB PDF was silently skipped in production because of this."
    )


# --- Cross-check: txt extraction works even when PDF+DOCX libs are missing


async def test_text_doc_works_without_pdf_or_docx_libs(monkeypatch, tmp_path):
    """Plain-text path has no library dependency — it must stay functional
    when pypdf/python-docx are absent. Guards against an over-eager gate
    that short-circuits everything on any missing lib.
    """
    monkeypatch.setattr(doc_mod, "_HAS_PYPDF", False)
    monkeypatch.setattr(doc_mod, "_HAS_DOCX", False)

    # Build a real file so _extract_plain has something to read, and stub
    # download_message so no Telegram round-trip happens.
    fake_txt = tmp_path / "doc_-1_10.md"
    fake_txt.write_text("hello from a markdown file\n")

    async def fake_download(_client, _tel_msg, out_path):
        # download_message normally writes the Telegram media to `out_path`
        # and returns the written path. Short-circuit with our canned file.
        return fake_txt

    monkeypatch.setattr(doc_mod, "download_message", fake_download)

    # Redirect tmp_dir so the stubbed file's name matches what enrich_document
    # expects to unlink on teardown (not strictly required, but tidier).
    from unread.config import get_settings

    settings = get_settings()
    settings.media.tmp_dir = tmp_path

    client = _StubClient(_fake_doc_tel_msg("note.md"))
    result = await doc_mod.enrich_document(_doc_message(), client=client, repo=_StubRepo())
    assert result is not None
    assert "hello from a markdown file" in result.content
