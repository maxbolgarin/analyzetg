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


def extract_text(path: Path) -> ExtractResult:
    """Read a plain-text / code / markup file.

    Tries UTF-8, UTF-16, CP1251, and Latin-1 in that order before
    falling back to UTF-8 with replacement. Same encoding ladder as
    `enrich/document.py:_extract_plain` so behaviour is identical
    across the local-file path and the Telegram-doc enrichment path.
    """
    import contextlib

    with path.open("rb") as f:
        raw = f.read()
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
        raise ValueError(
            "PDF has no extractable text — likely scanned. Run an OCR tool "
            "(e.g. `ocrmypdf input.pdf output.pdf`) and re-run on the OCR'd copy."
        )
    return ExtractResult(text=text, extra={"chars": len(text)})


def extract_docx(path: Path) -> ExtractResult:
    """Extract text from a DOCX via python-docx."""
    from unread.enrich.document import _extract_docx

    text = _extract_docx(path)
    if not text:
        raise ValueError("DOCX has no extractable text (empty document?)")
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
    """Transcribe an audio file via Whisper.

    Reuses `enrich/audio.py`'s `_transcribe_file` and
    `media/transcode.py:transcode_for_openai` (so chunking + format
    conversion stay consistent with the Telegram voice path). Raises
    `RuntimeError` with a friendly message when the OpenAI key is
    missing — Whisper has no non-OpenAI fallback in unread.
    """
    from openai import AsyncOpenAI

    from unread.config import get_settings
    from unread.enrich.audio import _transcribe_file
    from unread.media.transcode import transcode_for_openai

    settings = get_settings()
    if not settings.openai.api_key:
        raise RuntimeError(
            "Audio transcription requires an OpenAI key (Whisper). "
            "Run `unread tg init` to add one — your chat provider can stay non-OpenAI."
        )

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts = await transcode_for_openai(path, "voice", tmp_dir)
    audio_model = settings.openai.audio_model_default
    audio_lang = settings.openai.audio_language or None
    oai = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.request_timeout_sec)
    pieces: list[str] = []
    for part in parts:
        text = await _transcribe_file(oai, part, audio_model, audio_lang)
        pieces.append(text.strip())
    text = "\n".join(p for p in pieces if p)
    if not text:
        raise ValueError("Transcription produced no text — file may be silent or unreadable.")
    return ExtractResult(text=text, extra={"audio_model": audio_model, "chars": len(text)})


async def extract_video(path: Path) -> ExtractResult:
    """Transcribe a video file via ffmpeg → Whisper.

    `media.transcode.transcode_for_openai` already handles the
    extract-audio step when given media_type="video". Result shape
    matches :func:`extract_audio` so the caller can treat them
    interchangeably.
    """
    from openai import AsyncOpenAI

    from unread.config import get_settings
    from unread.enrich.audio import _transcribe_file
    from unread.media.transcode import transcode_for_openai

    settings = get_settings()
    if not settings.openai.api_key:
        raise RuntimeError(
            "Video transcription requires an OpenAI key (Whisper). "
            "Run `unread tg init` to add one — your chat provider can stay non-OpenAI."
        )

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts = await transcode_for_openai(path, "video", tmp_dir)
    audio_model = settings.openai.audio_model_default
    audio_lang = settings.openai.audio_language or None
    oai = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.request_timeout_sec)
    pieces: list[str] = []
    for part in parts:
        text = await _transcribe_file(oai, part, audio_model, audio_lang)
        pieces.append(text.strip())
    text = "\n".join(p for p in pieces if p)
    if not text:
        raise ValueError(
            "Transcription produced no text — video may have no audio track. "
            "ffmpeg required for video files; install via `brew install ffmpeg` (macOS) "
            "or your distro's package manager."
        )
    return ExtractResult(text=text, extra={"audio_model": audio_model, "chars": len(text)})


async def extract_image(path: Path) -> ExtractResult:
    """Describe an image via the vision model.

    Reuses `enrich/image.py:_vision_complete` so the prompt + model
    selection match the Telegram-photo enrichment path. The
    description is the body fed to the LLM; analyze produces a
    summary of the image content as if it were a text document.
    """
    import base64

    from openai import AsyncOpenAI

    from unread.config import get_settings
    from unread.enrich.image import _mime_from_path, _resolve_prompts, _vision_complete

    settings = get_settings()
    if not settings.openai.api_key:
        raise RuntimeError(
            "Image description requires an OpenAI key (vision). "
            "Run `unread tg init` to add one — your chat provider can stay non-OpenAI."
        )

    lang = (settings.locale.content_language or settings.locale.language or "en").lower()
    sys_prompt, user_prompt = _resolve_prompts(lang)
    mime = _mime_from_path(path)
    with path.open("rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    messages = [
        {"role": "system", "content": sys_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        },
    ]
    oai = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.request_timeout_sec)
    model = settings.enrich.vision_model
    resp = await _vision_complete(oai, model, messages)
    description = (resp.choices[0].message.content or "").strip()
    if not description:
        raise ValueError("Vision model returned no description — try a different image.")
    return ExtractResult(text=description, extra={"vision_model": model, "chars": len(description)})
