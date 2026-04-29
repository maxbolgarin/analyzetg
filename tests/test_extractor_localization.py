"""Extractor / enricher errors are routed through `i18n.t()`.

Pins the contract that the user-visible error messages from the file /
website / YouTube extractors are looked up by key, not hardcoded
English. Catches future regressions where someone adds a new extractor
and writes `raise ValueError("English text")` directly.

Runs with `locale.language=ru` so failed routing surfaces as English
text in the error message — the assertion checks for the Russian copy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from unread.config import reset_settings


@pytest.fixture
def ru_locale(monkeypatch):
    """Switch to Russian for the duration of one test."""
    # The `t()` helper resolves the language at call time from
    # `settings.locale.language`, so a singleton mutation is enough.
    from unread.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s.locale, "language", "ru")
    yield
    reset_settings()


def test_pdf_scanned_error_localized(ru_locale, tmp_path) -> None:
    """`extract_pdf` → ValueError uses i18n key `error_pdf_scanned`."""
    from unread.files.extractors import extract_pdf

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"")  # empty body forces the "no extractable text" branch

    with (
        patch("unread.enrich.document._extract_pdf", return_value=""),
        pytest.raises(ValueError) as excinfo,
    ):
        extract_pdf(pdf)

    msg = str(excinfo.value)
    # Russian copy mentions "OCR" and the localized intro fragment
    assert "OCR" in msg
    assert "PDF" in msg
    # Specifically the Russian phrase confirms i18n routing.
    assert "сканнового" in msg or "скан" in msg.lower()


def test_docx_empty_error_localized(ru_locale, tmp_path) -> None:
    from unread.files.extractors import extract_docx

    docx = tmp_path / "empty.docx"
    docx.write_bytes(b"")

    with (
        patch("unread.enrich.document._extract_docx", return_value=""),
        pytest.raises(ValueError) as excinfo,
    ):
        extract_docx(docx)

    msg = str(excinfo.value)
    assert "DOCX" in msg
    # Russian copy contains "нет извлекаемого текста" — checks i18n routing.
    assert "нет" in msg or "пуст" in msg.lower()


def test_audio_no_openai_error_localized(ru_locale, monkeypatch) -> None:
    """`extract_audio` raises a localized RuntimeError when no OpenAI key."""
    from unread.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s.openai, "api_key", "")  # force the gate
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from unread.files.extractors import extract_audio

    async def _run() -> None:
        await extract_audio(tmp_path_dummy())

    def tmp_path_dummy():
        # Real path doesn't matter — the gate fires before any I/O.
        from pathlib import Path

        return Path("/tmp/does-not-matter.mp3")

    import asyncio

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(_run())

    msg = str(excinfo.value)
    # Russian copy mentions OpenAI / Whisper as kept loanwords + Russian connectives.
    assert "OpenAI" in msg
    # Russian phrase confirms i18n routing.
    assert "требует" in msg or "ключ" in msg.lower()
