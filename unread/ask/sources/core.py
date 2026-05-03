"""Whole-document vs retrieval-fallback orchestrator for `unread ask <url|file>`.

The Telegram-archive ask path stays in `unread/ask/commands.py`. This
module owns the source-agnostic case: the caller has already extracted
text + citations from a YouTube transcript, a website page, a local
file, or stdin, and just needs an answer for a question over that text.

`cmd_ask_document` decides per call whether the extracted text fits in
one LLM call (whole-document path) or needs chunked retrieval
(retrieval-fallback path). The cutoff is `settings.ask.doc_full_text_cutoff_tokens`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from unread.ai.providers import resolve_chat_model
from unread.analyzer.openai_client import chat_complete, make_client
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.util.pricing import chat_cost
from unread.util.tokens import count_tokens

console = Console()


@dataclass(frozen=True)
class DocCitation:
    """Pointer back into the source document for a single chunk/segment.

    `uri` is the rendered citation target (file:// path, https://youtu.be/X?t=N,
    or a website URL). `label` is the short human-readable inline label
    ("p. 3", "00:14:22", etc.). `offset_start` / `offset_end` are byte
    offsets into the extracted text — used by the retrieval-fallback
    path to map a top-K chunk back to its source position.
    """

    uri: str
    label: str
    offset_start: int
    offset_end: int


def _build_doc_messages(
    *,
    source_text: str,
    source_label: str,
    question: str,
    answer_language: str,
    content_language: str,
) -> list[dict]:
    """One-shot system+user pair. `source_text` is either the full extracted
    text (whole-doc path) or the joined top-K chunks (retrieval path)."""
    system = (
        "Answer the user's question using ONLY the provided source text. "
        "If the answer is not in the source, say so. Cite by referring to "
        f"the source label '{source_label}' inline. The source content is "
        f"in {content_language}; respond in {answer_language}."
    )
    user = f"Source ({source_label}):\n\n{source_text}\n\n---\n\nQuestion: {question}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _split_into_segments(text: str, target_tokens: int) -> list[str]:
    """Split text first by double-newlines, then by single-newlines, then by words.

    Falls back progressively so plain paragraphs, line-oriented text, and
    continuous prose (no newlines at all) all produce reasonably sized segments.
    """
    # Try double-newline paragraphs first.
    segments = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(segments) > 1:
        return segments
    # Fall back to single-newline lines.
    segments = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(segments) > 1:
        return segments
    # Last resort: split into word-level windows of target_tokens size.
    words = text.split()
    result: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for w in words:
        wt = count_tokens(w)
        if buf and buf_tok + wt > target_tokens:
            result.append(" ".join(buf))
            buf, buf_tok = [], 0
        buf.append(w)
        buf_tok += wt
    if buf:
        result.append(" ".join(buf))
    return result or [text]


def _chunk_text(text: str, *, target_tokens: int) -> list[str]:
    """Naive paragraph-aware chunker for retrieval-fallback. ~target_tokens per chunk."""
    segments = _split_into_segments(text, target_tokens)
    chunks: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for seg in segments:
        ptok = count_tokens(seg)
        if buf and buf_tokens + ptok > target_tokens:
            chunks.append("\n\n".join(buf))
            buf, buf_tokens = [], 0
        buf.append(seg)
        buf_tokens += ptok
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _retrieve_top_k(chunks: list[str], question: str, *, k: int) -> list[tuple[int, str]]:
    """Cheap keyword-overlap ranking. Returns top-K (chunk_index, chunk_text) pairs.

    Stays in-process and deterministic so the test suite doesn't need
    network or embeddings. The richer keyword/embedding path under
    `unread.ask.retrieval` could be wired in later if doc-mode ask
    grows past the simple-overlap baseline.
    """
    q_terms = {t.lower() for t in question.split() if len(t) > 2}
    scored: list[tuple[int, int, str]] = []
    for i, c in enumerate(chunks):
        c_terms = {t.lower() for t in c.split() if len(t) > 2}
        score = len(q_terms & c_terms)
        scored.append((score, i, c))
    scored.sort(key=lambda r: (-r[0], r[1]))
    return [(i, c) for _s, i, c in scored[:k]]


async def cmd_ask_document(
    *,
    extracted_text: str,
    citations: list[DocCitation],
    source_label: str,
    source_id: str,
    content_hash: str,
    question: str,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
    no_followup: bool = False,
    semantic: bool = False,  # reserved for embedding-retrieval rewire (future task)
    build_index: bool = False,  # reserved for embedding-index build (future task)
    rerank: bool | None = None,  # reserved for cheap-model rerank pass (future task)
    limit: int = 200,
    show_retrieved: bool = False,
) -> None:
    """Answer `question` over `extracted_text`. Picks whole-doc vs retrieval based on token count.

    `citations` are accepted for future inline-link rendering but are not
    yet woven into the answer body — v1 cites by `source_label` only.
    Adapters should still build them so the wiring drop-in is mechanical.
    `no_followup` is accepted to match the chat-archive ask signature; the
    doc-mode path is one-shot so there's no follow-up loop to skip.
    """
    settings = get_settings()
    cutoff = settings.ask.doc_full_text_cutoff_tokens
    used_answer_language = (language or settings.locale.language or "en").lower()
    used_content_language = (
        content_language or settings.locale.content_language or used_answer_language
    ).lower()
    used_model = model or resolve_chat_model(settings)

    tokens = count_tokens(extracted_text)
    if tokens <= cutoff:
        source_text = extracted_text
        phase = "ask_doc_full"
    else:
        chunk_target = max(
            64, cutoff // 16
        )  # ~16 chunks per cutoff window; floor at 64 to avoid one-token shards
        chunks = _chunk_text(extracted_text, target_tokens=chunk_target)
        top_k = min(5, limit, len(chunks))  # cap at 5 chunks to keep context short
        top = _retrieve_top_k(chunks, question, k=top_k)
        if show_retrieved:
            for idx, chunk in top:
                console.print(f"[grey70]chunk #{idx}[/]: {chunk[:120]}…")
        source_text = "\n\n---\n\n".join(c for _i, c in top)
        phase = "ask_doc_retrieval"

    messages = _build_doc_messages(
        source_text=source_text,
        source_label=source_label,
        question=question,
        answer_language=used_answer_language,
        content_language=used_content_language,
    )

    # Cost guard — mirrors the chat-archive ask path so `--max-cost` works
    # symmetrically across all ask scopes.
    prompt_tokens = sum(count_tokens(m["content"], used_model) for m in messages)
    est_cost = chat_cost(used_model, prompt_tokens, 0, 2000, settings=settings)
    if est_cost is not None and max_cost is not None and est_cost > max_cost:
        console.print(
            f"[bold yellow]Estimated cost ${est_cost:.4f} exceeds --max-cost ${max_cost:.4f} "
            f"({prompt_tokens:,} prompt tokens × {used_model}, output capped at 2000).[/]"
        )
        if yes:
            console.print("[red]Aborting (--yes set).[/]")
            raise typer.Exit(2)
        from unread.util.prompt import confirm as _confirm

        if not _confirm("Run anyway?", default=False):
            console.print("[yellow]Aborted.[/]")
            raise typer.Exit(0)

    oai = make_client()
    async with open_repo(settings.storage.data_path) as repo:
        res = await chat_complete(
            oai,
            repo=repo,
            model=used_model,
            messages=messages,
            max_tokens=2000,
            context={
                "phase": phase,
                "source_label": source_label,
                "source_id": source_id,
                "content_hash": content_hash,
                "tokens": tokens,
            },
        )

    answer = (res.text or "").strip()
    if not answer:
        console.print("[red]Empty answer from model.[/]")
        return

    body = (
        f"# {question.strip()}\n\n"
        f"_Source: {source_label}, model {used_model}, ${float(res.cost_usd or 0):.4f}_\n\n"
        f"{answer}\n"
    )
    if output:
        output.write_text(body, encoding="utf-8")
        console.print(f"[grey70]Saved to[/] [bold]{output}[/]")
        if console_out:
            console.print(body)
    else:
        console.print(body)
