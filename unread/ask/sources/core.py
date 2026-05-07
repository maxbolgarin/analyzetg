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
from datetime import datetime
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


def _build_doc_system_prompt(
    *,
    source_label: str,
    report_language: str,
    source_language: str,
) -> str:
    """System prompt for doc-mode ask.

    Earlier versions said "Answer using ONLY the provided source text"
    which made the LLM refuse instruction-style queries like "проверь,
    правильно ли указаны источники" / "evaluate this argument" —
    nothing inside the source explicitly answers those. The relaxed
    prompt below treats the source as the *primary* reference but
    lets the model bring in external knowledge for verification,
    evaluation, comparison, and fact-checking. The model is asked to
    flag which claims come from the source vs. its own knowledge so
    the user can tell them apart.
    """
    system = (
        "You answer the user's questions about a single source document. The source is the "
        "primary reference: prefer to ground your answer in it, quote when useful, and cite "
        f"by referring to the source label '{source_label}' inline.\n\n"
        "When the user asks you to evaluate, verify, fact-check, compare, critique, or extend "
        "the source's claims, you may use your own background knowledge as well. In that case, "
        "make it clear which parts of the answer come from the source and which come from your "
        "own knowledge or analysis (e.g. prefix external claims with phrases like 'beyond the "
        "source:' or 'from general knowledge:').\n\n"
        "If you genuinely don't have enough information — neither in the source nor in your "
        f"training data — say so plainly. Respond in {report_language}, concisely and to the point."
    )
    if source_language:
        system += (
            f"\n\nThe source content is in {source_language}; treat citations and "
            "quotations from the source as that language."
        )
    return system


def _build_doc_messages(
    *,
    source_text: str,
    source_label: str,
    question: str,
    report_language: str,
    source_language: str,
) -> list[dict]:
    """One-shot system+user pair. `source_text` is either the full extracted
    text (whole-doc path) or the joined top-K chunks (retrieval path).

    `report_language` is the language the LLM writes the answer in.
    `source_language` is the Whisper-style hint about the input text;
    when empty, no source-language line is added — the LLM auto-detects.
    """
    system = _build_doc_system_prompt(
        source_label=source_label,
        report_language=report_language,
        source_language=source_language,
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
    source_kind: str = "doc",
    content_hash: str,
    question: str,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    no_console: bool = False,
    no_save: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
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

    On a TTY, after the first answer the user is prompted to continue
    chatting (Telegram-archive ask parity). Follow-up turns reuse the
    same source_text in the prompt so they hit OpenAI's prompt cache.
    Pass `no_followup=True` to skip the prompt (cron / scripts).

    Output behavior mirrors `unread <ref>` (analyze): rich-rendered
    Rule + header grid + Markdown body in the terminal, AND a saved
    markdown file under `~/.unread/reports/ask/<source_kind>/...` —
    both can be opted out of via `no_console` / `no_save`. Legacy
    `console_out` (the deprecated `--console` alias) maps to
    `no_save=True` for back-compat with internal callers.
    """
    settings = get_settings()
    cutoff = settings.ask.doc_full_text_cutoff_tokens
    _ui_language = (language or settings.locale.language or "en").lower()
    used_report_language = (report_language or settings.locale.report_language or _ui_language).lower()
    # Whisper-style source-content hint. Empty (the default) means the
    # LLM gets no explicit language line and infers from the source text.
    used_source_language = (
        (source_language if source_language is not None else settings.locale.content_language).strip().lower()
    )
    used_model = model or resolve_chat_model(settings)

    tokens = count_tokens(extracted_text)
    retrieval_meta: tuple[int, int] | None = None
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
        retrieval_meta = (top_k, len(chunks))

    # Legacy `console_out` (the deprecated `--console`/`-c` alias) means
    # "force terminal render even when --output is set". When the new
    # `no_save` / `no_console` flags aren't passed, fall back to the old
    # semantics so internal callers keep working: --output without
    # --console saves silently (no terminal render); --console keeps
    # the terminal render. Without --output, the user gets the new
    # default (render + save).
    if not no_save and not no_console and output is not None and not console_out:
        no_console = True

    oai = make_client()

    async def _ask_one_turn(q: str, history: list[tuple[str, str]]) -> tuple[str, float]:
        """Run one Q→A turn against the cached source text.

        Returns `(answer, cost_usd)`. `history` is the conversation so
        far; it's woven into the messages list as alternating user /
        assistant turns AFTER the system + initial user(source+question)
        pair so the source-bearing prefix stays byte-identical between
        turns and OpenAI's prompt cache hits.
        """
        messages = _build_doc_messages(
            source_text=source_text,
            source_label=source_label,
            question=history[0][0] if history else q,
            report_language=used_report_language,
            source_language=used_source_language,
        )
        if history:
            # First user message is the original question + source.
            # Append (assistant, user) pairs for each prior turn, then
            # the new follow-up question last.
            messages.append({"role": "assistant", "content": history[0][1]})
            for past_q, past_a in history[1:]:
                messages.append({"role": "user", "content": past_q})
                messages.append({"role": "assistant", "content": past_a})
            messages.append({"role": "user", "content": q})

        # Cost guard — applied per turn.
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

        # Progress status before the (potentially slow) LLM call so the
        # user sees something while waiting. Without this the terminal
        # hangs silently between "user pressed Enter" and "answer arrives".
        # Mirrors the Telegram-archive ask path's "→ Asking ..." line.
        turn_label = f"turn {len(history) + 1}" if history else f"turn 1 · whole-doc ({tokens:,} tokens)"
        cost_hint = f"~${est_cost:.4f}" if est_cost is not None else "cost unknown"
        console.print(
            f"[grey70]→ Asking[/] [bold]{used_model}[/] [grey70]"
            f"({turn_label}, {prompt_tokens:,} prompt tokens, {cost_hint})…[/]"
        )

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
                    "turn": len(history) + 1,
                },
            )
        answer_text = (res.text or "").strip()
        return answer_text, float(res.cost_usd or 0)

    # First turn: full header table + saved report.
    first_answer, first_cost = await _ask_one_turn(question, history=[])
    if not first_answer:
        console.print("[red]Empty answer from model.[/]")
        return

    from unread.analyzer.commands import _fmt_cost_precise
    from unread.ask.render import default_ask_path, truncate_value
    from unread.i18n import t as _t
    from unread.i18n import tf as _tf
    from unread.util.report_render import print_report_shell

    if phase == "ask_doc_full":
        mode_value = _t("ask_mode_whole_doc")
    else:
        k, total = retrieval_meta or (0, 0)
        mode_value = _tf("ask_mode_retrieval", k=k, n=total)

    header_rows: list[tuple[str, str]] = [
        (_t("ask_meta_source"), source_label),
        (_t("ask_meta_question"), truncate_value(question, 120)),
        (_t("ask_meta_mode"), mode_value),
        (_t("report_meta_model"), f"`{used_model}`"),
        (_t("ask_meta_tokens"), f"{tokens:,}"),
        (_t("report_meta_cost"), _fmt_cost_precise(first_cost)),
        (
            _t("report_meta_generated"),
            datetime.now().strftime("%Y-%m-%d %H:%M"),
        ),
    ]

    summary_line = (
        f"[bold cyan]{_t('report_summary_run')}[/] source={source_label} "
        f"mode={mode_value} tokens={tokens:,} cost=${first_cost:.4f}"
    )

    print_report_shell(
        summary_line=summary_line,
        title=source_label,
        meta_rows=header_rows,
        body_md=f"{first_answer}\n",
        output=output,
        default_path=default_ask_path(source_kind, source_label),
        no_console=no_console,
        no_save=no_save,
        plain_citations=settings.analyze.plain_citations,
    )

    # Follow-up loop — only when interactive AND the user didn't opt out.
    # The Telegram-archive ask path uses the same `_ask_continue`
    # single-keypress prompt; reuse it so the UX is identical across
    # every `unread ask` flavor.
    if no_followup or no_console:
        return
    if not _is_interactive():
        return

    from unread.ask.commands import _ask_continue

    if not await _ask_continue():
        return

    history: list[tuple[str, str]] = [(question, first_answer)]
    await _doc_followup_loop(
        history=history,
        ask_one_turn=_ask_one_turn,
        used_model=used_model,
    )


def _is_interactive() -> bool:
    """True if stdin is a TTY (not piped / redirected)."""
    import sys

    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


async def _doc_followup_loop(
    *,
    history: list[tuple[str, str]],
    ask_one_turn,
    used_model: str,
) -> None:
    """Multi-turn follow-up loop for doc-mode ask.

    Mirrors the loop in `unread/ask/commands.py:cmd_ask` (the chat-
    archive ask path). Each follow-up reuses the same source-bearing
    prompt prefix so OpenAI's prompt cache hits and the per-turn cost
    is dominated by the much-smaller history + new question.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings

    from unread.analyzer.commands import _flatten_citations, _fmt_cost_precise

    console.print(
        "\n[bold cyan]Interactive mode[/] — type a follow-up question (Esc / blank / Ctrl-D to exit)."
    )
    loop_kb = KeyBindings()

    @loop_kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result="")

    session: PromptSession = PromptSession(key_bindings=loop_kb)

    while True:
        try:
            follow = (await session.prompt_async(HTML("\n<ansicyan>> </ansicyan>"))).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not follow:
            break
        try:
            answer, cost = await ask_one_turn(follow, history)
        except typer.Exit as e:
            # Budget guard fires raise; in interactive mode that means
            # "skip this turn", not "kill the session".
            if e.exit_code == 0:
                continue
            raise
        if not answer:
            console.print("[red]Empty answer from model.[/]")
            continue
        history.append((follow, answer))

        from rich.markdown import Markdown
        from rich.rule import Rule

        console_body = f"{answer}\n"
        if get_settings().analyze.plain_citations:
            console_body = _flatten_citations(console_body)
        console.print(f"[grey70]turn {len(history)} · {used_model} · {_fmt_cost_precise(cost)}[/]")
        console.print(Rule("answer", style="cyan"))
        console.print(Markdown(console_body))
        console.print(Rule(style="cyan"))
