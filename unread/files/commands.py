"""End-to-end local-file / stdin analysis.

`cmd_analyze_file(ref, ...)` is the single entry point — `ref` is
either an absolute / relative file path OR `unread.cli._STDIN_REF_SENTINEL`
(`"<stdin>"`) for piped input. The flow mirrors `cmd_analyze_website`:

  1. Resolve the source (open the path, or read stdin).
  2. Detect kind (text / pdf / docx / audio / video / image / stdin).
  3. Extract text via the matching extractor.
  4. Hash content; check `local_files` cache.
  5. Build synthetic messages (header `#0` + paragraphs `#1..#N`).
  6. Run analysis through `analyzer.pipeline.run_analysis` with
     `source_kind="file"`.
  7. Render to terminal + save under
     `~/.unread/reports/files/<kind>/...`.

Citations land as `[#7](file:///abs/path)` so clicking opens the
source. Stdin runs use a literal `<stdin>` link (most terminals will
ignore it; that's fine — there's nothing to navigate to).
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from unread.analyzer.pipeline import AnalysisOptions, estimate_cost, run_analysis
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.files.extractors import (
    ExtractResult,
    FileKind,
    detect_kind,
    extract_audio,
    extract_docx,
    extract_image,
    extract_pdf,
    extract_text,
    extract_text_from_bytes,
    extract_video,
)
from unread.files.paths import file_report_path
from unread.i18n import t as _t
from unread.models import Message
from unread.util.logging import get_logger

console = Console()
log = get_logger(__name__)

# Soft cap on per-paragraph length when chunking the extracted text.
# Matches `website/extract.py`'s default — keeps the synthetic-message
# count stable across kinds so chunker / token budgeting work the same.
_MAX_PARAGRAPH_CHARS = 3500


def _segment_into_paragraphs(text: str, *, max_chars: int = _MAX_PARAGRAPH_CHARS) -> list[str]:
    """Split extracted text into LLM-friendly paragraph chunks.

    Prefers blank-line splits, falls through to single-newline splits,
    then forces hard cuts to keep each chunk under `max_chars`. Same
    ladder the website extractor uses — duplicate by design (cheap, and
    importing the website internal would couple two analyzers that
    should be free to diverge).
    """
    text = (text or "").strip()
    if not text:
        return []
    # Try blank-line paragraph splits first.
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    out: list[str] = []
    for block in blocks:
        if len(block) <= max_chars:
            out.append(block)
            continue
        # Block too big — try line splits.
        for raw_line in block.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if len(line) <= max_chars:
                out.append(line)
                continue
            # Single line is too long — hard chunks.
            for i in range(0, len(line), max_chars):
                out.append(line[i : i + max_chars])
    return out


def _hash_content(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _file_id_for_path(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _file_id_for_stdin(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


# Cap stdin input to keep `cat huge.bin | unread` from OOMing the process.
# 100 MB covers any realistic transcript / log dump while still fitting in
# memory on a constrained machine. A user feeding a larger file should
# pre-extract the relevant slice (`head -c 100M file | unread`) — that's
# more honest than us silently truncating *and* charging them to analyze
# only the first chunk. Anything past the cap is dropped with a warning.
_MAX_STDIN_BYTES = 100_000_000


def _read_stdin_bytes() -> tuple[bytes, bool]:
    """Slurp stdin, capped at `_MAX_STDIN_BYTES`. Used only when
    `cmd_analyze_file` was invoked with the stdin sentinel — never opens
    a TTY (the cli-layer detector guards against that).

    Returns ``(data, truncated)``. When ``truncated`` is True, callers
    must surface the cap in user-visible output so the LLM (and human)
    aren't analyzing a silently-cut input.
    """
    # Read one extra byte so we can detect overflow without buffering
    # gigabytes — if the read returns more than the cap, we know the
    # source had more data even though we drop the tail.
    data = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(data) > _MAX_STDIN_BYTES:
        log.warning(
            "stdin.truncated",
            cap_bytes=_MAX_STDIN_BYTES,
            received_bytes=len(data),
        )
        return data[:_MAX_STDIN_BYTES], True
    return data, False


def _file_uri(path: Path) -> str:
    """Return the file:// URI for `path`. Plain string ops to avoid
    pulling in pathlib's `as_uri` on Windows (which has subtle
    differences across versions)."""
    abs_path = str(path.resolve())
    if abs_path.startswith("/"):
        return f"file://{abs_path}"
    # Windows path: prepend a slash to the volume so it forms a valid URI.
    return "file:///" + abs_path.replace("\\", "/")


def _meta_header(
    *, name: str, kind: FileKind | str, path_uri: str, paragraphs_count: int, extra: dict | None
) -> str:
    """Synthetic message #0 — visible to the LLM as the file's metadata.

    Matches the layout of `website/commands.py:_meta_header`: a list of
    `Key: value` lines bracketed by `=== File: ... ===`. The base
    prompt's "preamble parsing" rules already understand this shape
    (see `presets/<lang>/_base.md`).
    """
    lines = [f"=== File: {name} ===", f"Kind: {kind}", f"Paragraphs: {paragraphs_count}"]
    if extra:
        for k, v in extra.items():
            if v is None or v == "":
                continue
            lines.append(f"{k.replace('_', ' ').capitalize()}: {v}")
    if path_uri:
        lines.append(f"Source: {path_uri}")
    return "\n".join(lines)


def _build_synthetic_messages(
    *,
    name: str,
    kind: str,
    path_uri: str,
    paragraphs: list[str],
    extra: dict | None,
) -> list[Message]:
    """Assemble the synthetic message list the analyzer expects.

    Header is `#0` (sender = the file's basename so the LLM has a
    visible attribution); paragraphs are `#1..#N`. Dates climb 1 second
    per message starting from "now" so the chunker's stable-sort is
    deterministic.
    """
    now = datetime.now()
    sender = name or "file"
    header = Message(
        chat_id=0,
        msg_id=0,
        date=now,
        sender_id=0,
        sender_name=sender,
        text=_meta_header(
            name=name, kind=kind, path_uri=path_uri, paragraphs_count=len(paragraphs), extra=extra
        ),
    )
    msgs: list[Message] = [header]
    for i, body in enumerate(paragraphs, start=1):
        msgs.append(
            Message(
                chat_id=0,
                msg_id=i,
                date=now,
                sender_id=0,
                sender_name=sender,
                text=body,
            )
        )
    return msgs


async def _extract_for_kind(path: Path, kind: FileKind) -> ExtractResult:
    """Dispatch to the right extractor based on detected kind."""
    if kind == "text":
        return extract_text(path)
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "docx":
        return extract_docx(path)
    if kind == "audio":
        return await extract_audio(path)
    if kind == "video":
        return await extract_video(path)
    if kind == "image":
        return await extract_image(path)
    raise typer.BadParameter(
        f"Unsupported file type {path.suffix!r}. Supported: text/code/markup, "
        ".pdf, .docx, audio (mp3/m4a/wav/ogg/flac/opus), video (mp4/mov/mkv/webm), "
        "image (png/jpg/webp/gif)."
    )


async def cmd_analyze_file(
    *,
    ref: str,
    preset: str | None,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_cache: bool = False,
    max_cost: float | None = None,
    dry_run: bool = False,
    self_check: bool = False,
    post_to: str | None = None,
    post_saved: bool = False,
    language: str = "en",
    content_language: str = "en",
    yes: bool = False,
) -> None:
    """Analyze a local file or stdin. Same flag set as the chat / website paths."""
    from unread.analyzer.commands import (
        _load_preset_for_commands,
        _post_to_chat,
        _print_and_write,
        _self_check,
    )
    from unread.cli import _STDIN_REF_SENTINEL, _resolve_local_file_path

    settings = get_settings()
    effective_preset = preset or "summary"

    is_stdin = ref == _STDIN_REF_SENTINEL
    stdin_truncated = False
    if is_stdin:
        raw, stdin_truncated = _read_stdin_bytes()
        if not raw.strip():
            console.print(f"[red]{_t('files_no_stdin_input')}[/]")
            raise typer.Exit(2)
        if stdin_truncated:
            cap_mb = _MAX_STDIN_BYTES // 1_000_000
            console.print(f"[yellow]{_t('files_stdin_truncated').format(cap_mb=cap_mb)}[/]")
        result_extract = extract_text_from_bytes(raw, label="stdin")
        kind: str = "stdin"
        name = "stdin"
        abs_path = ""
        extension = ""
        path_uri = "<stdin>"
        file_id = _file_id_for_stdin(raw)
    else:
        path = _resolve_local_file_path(ref)
        if path is None:
            console.print(f"[red]{_t('files_not_found').format(ref=repr(ref))}[/]")
            raise typer.Exit(2)
        kind = detect_kind(path)
        if kind == "unknown":
            console.print(f"[red]{_t('files_unsupported_kind').format(ext=repr(path.suffix))}[/]")
            raise typer.Exit(2)
        name = path.name
        abs_path = str(path)
        extension = path.suffix.lower()
        path_uri = _file_uri(path)
        file_id = _file_id_for_path(path)
        try:
            result_extract = await _extract_for_kind(path, kind)
        except (RuntimeError, ValueError) as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(2) from e

    paragraphs = _segment_into_paragraphs(result_extract.text)
    if not paragraphs:
        console.print(f"[red]{_t('files_empty_file')}[/]")
        raise typer.Exit(2)
    content_hash = _hash_content("\n\n".join(paragraphs))

    async with open_repo(settings.storage.data_path) as repo:
        # Cache lookup: if the file_id row exists with matching content_hash,
        # skip re-extracting (free re-runs on identical files).
        if not no_cache and not is_stdin:
            cached = await repo.get_local_file(file_id)
            if cached and cached.get("content_hash") == content_hash:
                console.print(f"[grey70]Using cached extraction for {name}[/]")
                # paragraphs already match — no re-write needed.
        await repo.put_local_file(
            file_id=file_id,
            abs_path=abs_path,
            name=name,
            kind=kind,
            extension=extension,
            content_hash=content_hash,
            paragraphs=paragraphs,
            extract_size=result_extract.extra.get("bytes") if result_extract.extra else None,
        )

        # Surface stdin truncation in the synthetic header — the LLM
        # otherwise gets no signal that its input was clipped at the cap.
        meta_extra = dict(result_extract.extra or {})
        if stdin_truncated:
            meta_extra["truncated_at_bytes"] = _MAX_STDIN_BYTES
        messages = _build_synthetic_messages(
            name=name,
            kind=kind,
            path_uri=path_uri,
            paragraphs=paragraphs,
            extra=meta_extra,
        )

        loaded_preset = _load_preset_for_commands(effective_preset, prompt_file, language=content_language)

        if dry_run:
            n = len(messages)
            if loaded_preset is None:
                console.print(f"[bold]Dry run: {n} synthetic msgs / preset={effective_preset}[/]")
                return
            lo, hi = estimate_cost(n_messages=n, preset=loaded_preset, settings=settings)
            console.print(
                f"[bold]Dry run: file={name} kind={kind} paragraphs={len(paragraphs)} "
                f"preset={effective_preset} final={loaded_preset.final_model} "
                f"filter={loaded_preset.filter_model}[/]"
            )
            if hi is not None:
                console.print(f"  Estimated cost: ${lo or 0.0:.4f} – ${hi:.4f}")
            else:
                console.print("  [yellow]Cost estimate unavailable (missing pricing entry)[/]")
            return

        if max_cost is not None and loaded_preset is not None:
            lo, hi = estimate_cost(n_messages=len(messages), preset=loaded_preset, settings=settings)
            if hi is not None and hi > max_cost:
                console.print(
                    f"[bold yellow]Estimated upper-bound cost ${hi:.4f} exceeds --max-cost ${max_cost:.4f}[/]"
                )
                if yes:
                    console.print("[red]Aborting (--yes set).[/]")
                    raise typer.Exit(2)
                from unread.util.prompt import confirm as _confirm

                if not _confirm("Run anyway?", default=False):
                    console.print("[yellow]Aborted.[/]")
                    raise typer.Exit(0)

        opts = AnalysisOptions(
            preset=effective_preset,
            prompt_file=prompt_file,
            model_override=model,
            filter_model_override=filter_model,
            use_cache=not no_cache,
            include_transcripts=True,
            min_msg_chars=0,  # synthetic header is short; never drop it
            local_file_id=file_id,
            local_file_content_hash=content_hash,
            source_kind="file",
        )

        # Citations link back to the source: `[#7](file:///abs/path)`.
        # Most editors ignore paragraph anchors but at least re-open the file.
        link_template = path_uri

        console.print(f"[grey70]{_t('running_analysis')}[/]")
        result = await run_analysis(
            repo=repo,
            chat_id=0,
            thread_id=None,
            title=name,
            opts=opts,
            messages=messages,
            language=language,
            content_language=content_language,
            link_template_override=link_template,
        )

        if self_check and result.final_result and messages:
            verification = await _self_check(
                result=result,
                messages=messages,
                repo=repo,
                content_language=content_language,
            )
            if verification:
                heading = _t("verification_heading", language)
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + verification

        if output is None and not console_out:
            output_path: Path | None = file_report_path(
                file_id=file_id,
                name=name,
                kind=kind,
                preset=effective_preset,
            )
        else:
            output_path = output

        _print_and_write(
            result,
            output=output_path,
            title=name,
            no_save=console_out,
        )

        post_target = post_to if post_to else ("me" if post_saved else None)
        if post_target and result.msg_count > 0:
            try:
                await _post_to_chat(
                    None,
                    repo,
                    result,
                    title=name,
                    target=post_target,  # type: ignore[arg-type]
                )
            except Exception as e:
                log.warning("file.post_to.failed", target=post_target, err=str(e)[:200])
                console.print(f"[yellow]Couldn't post to {post_target}: {e}[/]")
