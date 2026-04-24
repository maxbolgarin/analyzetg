"""Document enricher: extract text from pdf / docx / md / txt / source code.

Detects supported files by filename + mime info carried on the Telethon
`doc` media type. Extracted text is truncated to `max_doc_chars` and
cached under `kind='doc_extract'` keyed by document_id.

Unlike image and link enrichers, this one doesn't need an OpenAI call for
the *happy path* — it just pulls text out of the file. For oversized docs,
a cheap summarization pass via `filter_model` is used (future work); v1
simply truncates with a "[truncated]" marker so the analyzer sees signal
rather than silence.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.base import EnrichResult
from analyzetg.media.download import download_message
from analyzetg.models import Message
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)

_TEXT_EXTS = {
    "txt",
    "md",
    "markdown",
    "rst",
    "log",
    "csv",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "cfg",
    # Common source code — extracted verbatim so the analyzer can reason about it.
    "py",
    "js",
    "ts",
    "tsx",
    "jsx",
    "go",
    "rs",
    "java",
    "kt",
    "swift",
    "c",
    "h",
    "cpp",
    "hpp",
    "cs",
    "rb",
    "php",
    "sh",
    "bash",
    "zsh",
    "sql",
    "html",
    "css",
    "scss",
}


def _ext_of(tel_msg) -> str:
    """Best-effort filename extension from a Telethon doc message."""
    doc = getattr(tel_msg, "document", None) or getattr(getattr(tel_msg, "media", None), "document", None)
    if doc is None:
        return ""
    for attr in getattr(doc, "attributes", []) or []:
        name = getattr(attr, "file_name", None)
        if name:
            return Path(name).suffix.lower().lstrip(".")
    mime = getattr(doc, "mime_type", "") or ""
    if "pdf" in mime:
        return "pdf"
    if "wordprocessingml" in mime or "msword" in mime:
        return "docx"
    return ""


def _extract_pdf(path: Path, *, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    running = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as e:  # pypdf can choke on unusual PDFs; skip page.
            log.debug("enrich.doc.pdf_page_error", err=str(e)[:200])
            continue
        text = text.strip()
        if not text:
            continue
        parts.append(text)
        running += len(text)
        if running >= max_chars:
            break
    return "\n\n".join(parts)


def _extract_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(paras)


def _extract_plain(path: Path) -> str:
    with path.open("rb") as f:
        raw = f.read()
    for enc in ("utf-8", "utf-16", "cp1251", "latin-1"):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode(enc)
    return raw.decode("utf-8", errors="replace")


async def enrich_document(
    msg: Message,
    *,
    client: TelegramClient,
    repo: Repo,
) -> EnrichResult | None:
    """Extract text from a document attached to `msg`. None = not extractable.

    Skips documents larger than `settings.enrich.max_doc_bytes` so a 500 MB
    zip doesn't quietly hang the analyzer. Caches the extracted text
    (truncated to `max_doc_chars`) under doc_id so the same PDF shared
    across chats is processed once.
    """
    settings = get_settings()
    if msg.media_type != "doc" or msg.media_doc_id is None:
        return None

    cached = await repo.get_media_enrichment(msg.media_doc_id, "doc_extract")
    if cached:
        content = cached.get("content") or ""
        msg.extracted_text = content
        return EnrichResult(kind="doc_extract", content=content, cache_hit=True)

    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("enrich.doc.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    ext = _ext_of(tel_msg)
    if not ext or (ext not in {"pdf", "docx"} and ext not in _TEXT_EXTS):
        return None

    # Enforce size cap before download to avoid pulling huge binaries.
    doc = getattr(tel_msg, "document", None) or getattr(getattr(tel_msg, "media", None), "document", None)
    size = int(getattr(doc, "size", 0) or 0)
    if size and size > settings.enrich.max_doc_bytes:
        log.warning(
            "enrich.doc.too_large",
            chat_id=msg.chat_id,
            msg_id=msg.msg_id,
            size=size,
            max=settings.enrich.max_doc_bytes,
        )
        return None

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / f"doc_{msg.chat_id}_{msg.msg_id}.{ext}"
    downloaded: Path | None = None
    try:
        downloaded = await download_message(client, tel_msg, src)
        try:
            if ext == "pdf":
                text = _extract_pdf(downloaded, max_chars=settings.enrich.max_doc_chars)
            elif ext == "docx":
                text = _extract_docx(downloaded)
            else:
                text = _extract_plain(downloaded)
        except Exception as e:
            log.warning(
                "enrich.doc.extract_failed",
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                ext=ext,
                err=str(e)[:200],
            )
            return None

        text = (text or "").strip()
        if not text:
            return None
        truncated = False
        if len(text) > settings.enrich.max_doc_chars:
            text = text[: settings.enrich.max_doc_chars] + "\n…[truncated]"
            truncated = True

        await repo.put_media_enrichment(
            int(msg.media_doc_id),
            "doc_extract",
            text,
            extra_json=f'{{"ext": "{ext}", "truncated": {str(truncated).lower()}}}',
        )
        msg.extracted_text = text
        return EnrichResult(kind="doc_extract", content=text)
    finally:
        if downloaded is not None:
            with contextlib.suppress(FileNotFoundError):
                downloaded.unlink()
