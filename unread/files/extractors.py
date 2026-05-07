"""Per-kind file → plain-text extractors.

`detect_kind(path)` classifies a file by extension. `extract_text(path,
kind)` and `extract_text_from_bytes(data, name)` return the extracted
body as a single string. Audio / video / image extraction is async
because it hits OpenAI (Whisper / vision); text and PDF / DOCX
extraction is sync (pure local I/O + library calls).

Extractors are deliberately thin wrappers around the existing
`enrich/audio.py`, `enrich/image.py`, `enrich/document.py`, and
`media/transcode.py` helpers — no new I/O patterns, no new SDKs. The
only new code here is the kind-detection table and a stdin-friendly
extractor for piped text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from unread.i18n import t as _t

FileKind = Literal["text", "pdf", "docx", "audio", "video", "image", "unknown"]


# Extension → kind table. Plain-text bucket is intentionally permissive —
# anything that isn't binary tends to be readable, and the LLM can chew
# through configuration / log / source files without preprocessing.
_TEXT_EXT: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".log",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".html",
        ".htm",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        # Common code extensions — feeds the LLM exactly what's in the
        # file. No tree-sitter, no embeddings; the file is treated as a
        # plain text document for analysis.
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".java",
        ".kt",
        ".scala",
        ".swift",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".lua",
        ".r",
        ".dart",
        ".vue",
        ".svelte",
        ".tex",
    }
)
_PDF_EXT: frozenset[str] = frozenset({".pdf"})
_DOCX_EXT: frozenset[str] = frozenset({".docx"})
_AUDIO_EXT: frozenset[str] = frozenset(
    {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma"}
)
_VIDEO_EXT: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"})
_IMAGE_EXT: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


def detect_kind(path: Path) -> FileKind:
    """Classify a path by file extension. Filename-only — does not stat."""
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXT:
        return "text"
    if suffix in _PDF_EXT:
        return "pdf"
    if suffix in _DOCX_EXT:
        return "docx"
    if suffix in _AUDIO_EXT:
        return "audio"
    if suffix in _VIDEO_EXT:
        return "video"
    if suffix in _IMAGE_EXT:
        return "image"
    return "unknown"


@dataclass(slots=True)
class ExtractResult:
    """Output of an extractor.

    `text` is the body fed to the LLM. `extra` carries kind-specific
    metadata that the orchestrator surfaces in the report header
    (audio duration, image vision-model name, etc.).
    """

    text: str
    extra: dict[str, str | int | float] | None = None


# ---- sync extractors (text / PDF / DOCX) -------------------------------


# Hard cap on local file reads so a `unread ./giant.log` doesn't OOM
# the process. 100 MB is generous for a code/text file but well below
# disaster on constrained machines.
_MAX_EXTRACT_BYTES = 100 * 1024 * 1024


def extract_text(path: Path) -> ExtractResult:
    """Read a plain-text / code / markup file.

    Tries UTF-8, UTF-16, CP1251, and Latin-1 in that order before
    falling back to UTF-8 with replacement. Same encoding ladder as
    `enrich/document.py:_extract_plain` so behaviour is identical
    across the local-file path and the Telegram-doc enrichment path.

    Refuses files over `_MAX_EXTRACT_BYTES` to avoid OOM. Pre-prod
    review: a 1+ GB log file used to be slurped into memory in one
    call.
    """
    import contextlib

    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError(f"cannot stat {path}: {e}") from e
    if size > _MAX_EXTRACT_BYTES:
        mb = size / (1024 * 1024)
        cap_mb = _MAX_EXTRACT_BYTES // (1024 * 1024)
        raise ValueError(
            f"file too large: {path} is {mb:.1f} MiB (cap {cap_mb} MiB). "
            "Pre-process or split before passing to unread."
        )
    # Chunked read with the cap enforced at the read site, not just
    # via the upfront stat. Defends against TOCTOU growth between
    # stat() and read() on log files / streamed mounts that report a
    # smaller size than what arrives. 1 MiB chunks keep Python's
    # bytes-concat overhead negligible.
    chunks: list[bytes] = []
    bytes_read = 0
    with path.open("rb") as f:
        while bytes_read <= _MAX_EXTRACT_BYTES:
            chunk = f.read(min(1024 * 1024, _MAX_EXTRACT_BYTES + 1 - bytes_read))
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)
    if bytes_read > _MAX_EXTRACT_BYTES:
        cap_mb = _MAX_EXTRACT_BYTES // (1024 * 1024)
        raise ValueError(
            f"file grew past cap during read: {path} exceeded {cap_mb} MiB. "
            "Pre-process or split before passing to unread."
        )
    raw = b"".join(chunks)
    for enc in ("utf-8", "utf-16", "cp1251", "latin-1"):
        with contextlib.suppress(UnicodeDecodeError):
            return ExtractResult(text=raw.decode(enc), extra={"bytes": len(raw)})
    return ExtractResult(text=raw.decode("utf-8", errors="replace"), extra={"bytes": len(raw)})


def extract_pdf(path: Path, *, max_chars: int = 200_000) -> ExtractResult:
    """Extract text from a PDF via pypdf.

    `max_chars` caps the running concatenation so a 1000-page doc
    doesn't blow the LLM's context. Same approach as the Telegram-doc
    enricher's `_extract_pdf` — borrowed wholesale to avoid two
    code paths drifting.
    """
    from unread.enrich.document import _extract_pdf

    text = _extract_pdf(path, max_chars=max_chars)
    if not text:
        raise ValueError(_t("error_pdf_scanned"))
    return ExtractResult(text=text, extra={"chars": len(text)})


def extract_docx(path: Path) -> ExtractResult:
    """Extract text from a DOCX via python-docx."""
    from unread.enrich.document import _extract_docx

    text = _extract_docx(path)
    if not text:
        raise ValueError(_t("error_docx_empty"))
    return ExtractResult(text=text, extra={"chars": len(text)})


def extract_text_from_bytes(data: bytes, label: str = "stdin") -> ExtractResult:
    """Decode in-memory bytes as text. Used for the stdin path.

    Same encoding ladder as :func:`extract_text` so a `cat foo.txt | unread`
    invocation behaves identically to `unread ./foo.txt`.
    """
    import contextlib

    for enc in ("utf-8", "utf-16", "cp1251", "latin-1"):
        with contextlib.suppress(UnicodeDecodeError):
            return ExtractResult(text=data.decode(enc), extra={"bytes": len(data), "source": label})
    return ExtractResult(
        text=data.decode("utf-8", errors="replace"),
        extra={"bytes": len(data), "source": label},
    )


# ---- async extractors (Whisper / vision) -------------------------------


async def extract_audio(path: Path) -> ExtractResult:
    """Transcribe an audio file via the audio slot's resolved provider.

    Reuses `enrich/audio.py`'s `_transcribe_file` and
    `media/transcode.py:transcode_for_openai` (so chunking + format
    conversion stay consistent with the Telegram voice path). Raises
    `RuntimeError` with a friendly message when the resolved provider
    has no key configured.
    """
    from unread.ai.providers import (
        ProviderUnavailableError as _ProviderUnavailableError,
    )
    from unread.ai.providers import (
        make_audio_client as _make_audio_client,
    )
    from unread.ai.providers import (
        resolve_audio as _resolve_audio,
    )
    from unread.config import get_settings
    from unread.enrich.audio import _transcribe_file
    from unread.media.download import transcode_for_openai

    settings = get_settings()
    audio_provider, audio_model = _resolve_audio(settings)
    try:
        oai = _make_audio_client(audio_provider, settings)
    except _ProviderUnavailableError as e:
        if audio_provider == "openai":
            raise RuntimeError(_t("error_audio_no_openai")) from e
        raise RuntimeError(str(e)) from e

    tmp_dir = settings.media.tmp_dir
    from unread.util.fsmode import ensure_private_dir

    ensure_private_dir(tmp_dir)
    parts = await transcode_for_openai(path, "voice", tmp_dir)
    audio_lang = settings.openai.audio_language or None
    pieces: list[str] = []
    for part in parts:
        text = await _transcribe_file(oai, part, audio_model, audio_lang)
        pieces.append(text.strip())
    text = "\n".join(p for p in pieces if p)
    if not text:
        raise ValueError(_t("error_audio_silent"))
    return ExtractResult(
        text=text,
        extra={"audio_provider": audio_provider, "audio_model": audio_model, "chars": len(text)},
    )


async def extract_video(path: Path) -> ExtractResult:
    """Transcribe a video file via ffmpeg → audio slot's resolved provider.

    `media.transcode.transcode_for_openai` already handles the
    extract-audio step when given media_type="video". Result shape
    matches :func:`extract_audio` so the caller can treat them
    interchangeably.
    """
    from unread.ai.providers import (
        ProviderUnavailableError as _ProviderUnavailableError,
    )
    from unread.ai.providers import (
        make_audio_client as _make_audio_client,
    )
    from unread.ai.providers import (
        resolve_audio as _resolve_audio,
    )
    from unread.config import get_settings
    from unread.enrich.audio import _transcribe_file
    from unread.media.download import transcode_for_openai

    settings = get_settings()
    audio_provider, audio_model = _resolve_audio(settings)
    try:
        oai = _make_audio_client(audio_provider, settings)
    except _ProviderUnavailableError as e:
        if audio_provider == "openai":
            raise RuntimeError(_t("error_video_no_openai")) from e
        raise RuntimeError(str(e)) from e

    tmp_dir = settings.media.tmp_dir
    from unread.util.fsmode import ensure_private_dir

    ensure_private_dir(tmp_dir)
    parts = await transcode_for_openai(path, "video", tmp_dir)
    audio_lang = settings.openai.audio_language or None
    pieces: list[str] = []
    for part in parts:
        text = await _transcribe_file(oai, part, audio_model, audio_lang)
        pieces.append(text.strip())
    text = "\n".join(p for p in pieces if p)
    if not text:
        raise ValueError(_t("error_video_silent"))
    return ExtractResult(
        text=text,
        extra={"audio_provider": audio_provider, "audio_model": audio_model, "chars": len(text)},
    )


async def extract_image(path: Path) -> ExtractResult:
    """Describe an image via the vision slot's resolved provider.

    Routes through the same vision adapter that enriches Telegram
    photos so the prompt + model + provider selection stay consistent
    across input sources. The description is the body fed to the LLM;
    analyze produces a summary as if the image were a text document.
    """
    from unread.ai.providers import (
        ProviderUnavailableError as _ProviderUnavailableError,
    )
    from unread.ai.providers import (
        resolve_vision as _resolve_vision,
    )
    from unread.ai.vision_provider import make_vision_provider
    from unread.config import get_settings
    from unread.enrich.image import _mime_from_path, _resolve_prompts

    settings = get_settings()
    vision_provider, model = _resolve_vision(settings)
    try:
        adapter = make_vision_provider(vision_provider, settings)
    except _ProviderUnavailableError as e:
        if vision_provider == "openai":
            raise RuntimeError(_t("error_image_no_openai")) from e
        raise RuntimeError(str(e)) from e

    # Image-description prompt is fed to the LLM; pick the report language
    # so the description matches the rest of the analysis output.
    lang = (settings.locale.report_language or settings.locale.language or "en").lower()
    sys_prompt, user_prompt = _resolve_prompts(lang)
    mime = _mime_from_path(path)
    with path.open("rb") as f:
        image_bytes = f.read()
    result = await adapter.describe_image(
        model=model,
        image_bytes=image_bytes,
        mime_type=mime,
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
        max_tokens=400,
        temperature=0.2,
    )
    description = result.text
    if not description:
        raise ValueError(_t("error_image_empty"))
    return ExtractResult(
        text=description,
        extra={"vision_provider": vision_provider, "vision_model": model, "chars": len(description)},
    )
