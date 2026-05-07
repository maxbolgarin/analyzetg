"""`unread ask` command — answer a question over the local Telegram corpus.

Pipeline:
  1. Resolve scope (chat / folder / global) → list of chat_ids.
  2. Retrieve top-N relevant messages from the local DB (no Telegram RPCs).
  3. Format with the existing `analyzer/formatter.py` so the prompt has the
     same `[timestamp #msg_id] author:` shape the analysis presets use.
  4. Single LLM call with a Q&A system prompt; the model is asked to cite
     msg_ids inline.
  5. Print to terminal and/or save to a file.

No map-reduce: top-N is bounded by `--limit` (default 200) which fits
comfortably in any flagship's context window. If the user wants to ask
a year-of-history question, retrieval keeps the chunk small.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from unread.analyzer.formatter import build_link_template, format_messages
from unread.analyzer.openai_client import chat_complete, make_client
from unread.ask.retrieval import retrieve_messages, tokenize_question
from unread.config import get_settings
from unread.core.paths import compute_window
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.tg.client import tg_client
from unread.tg.folders import list_folders, resolve_folder
from unread.tg.resolver import resolve as resolve_ref
from unread.util.logging import get_logger

console = Console()
log = get_logger(__name__)


async def _resolve_ask_ref(
    client,
    repo,
    ref: str,
    *,
    resolve_fn=None,
) -> tuple[int, int | None, int | None]:
    """Resolve a positional <ref> for ask: URL / @user / fuzzy / numeric.

    Returns `(chat_id, thread_id, msg_id)`. URL forms like
    `t.me/c/<id>/<topic>/<msg>` populate thread_id and msg_id; flat
    references leave both as None. Caller decides whether to honour
    thread_id (only if the user didn't pass --thread on the CLI).

    `resolve_fn` is for tests and for callers that want to swap the
    resolver (e.g. fixture replay).
    """
    fn = resolve_fn or resolve_ref
    resolved = await fn(client, repo, ref)
    return resolved.chat_id, resolved.thread_id, resolved.msg_id


# Per-language system prompt for the answer call. Picked at runtime from
# `settings.locale.language` (or the `--language` flag override). The model
# can also switch language to match the question if it differs.
_SYSTEM_PROMPT: dict[str, str] = {
    "en": (
        "You answer the user's questions over their Telegram message archive. "
        "Rely EXCLUSIVELY on the provided messages. Do not invent facts — if the "
        "answer isn't in the data, say so. "
        "Cite every statement with a markdown link [#<msg_id>](<link>), where "
        "link is built by substituting msg_id into the template from the "
        "'Message link:' line of the corresponding chat group. If the template "
        "is missing for a group, write just #<msg_id>. Answer in the question's "
        "language, concisely and to the point."
    ),
    "ru": (
        "Ты отвечаешь на вопросы пользователя по его архиву Telegram-сообщений. "
        "Опирайся ИСКЛЮЧИТЕЛЬНО на приведённые сообщения. Не выдумывай фактов — "
        "если ответа нет в данных, так и скажи. "
        "Каждое утверждение цитируй markdown-ссылкой [#<msg_id>](<link>), где "
        "link построен подстановкой msg_id в шаблон из строки "
        "'Ссылка на сообщение:' соответствующей группы сообщений. Если шаблона "
        "для группы нет, пиши просто #<msg_id>. Отвечай на языке вопроса, "
        "кратко, по существу."
    ),
}


def _resolve_system_prompt(language: str) -> str:
    return _SYSTEM_PROMPT.get(language, _SYSTEM_PROMPT["en"])


def _chat_slot_provider_has_key(settings, provider: str) -> bool:  # type: ignore[no-untyped-def]
    """True iff `provider` has a usable key for the chat slot.

    Mirrors :func:`unread.cli._active_provider_credentials_present` for
    the `ask` flow but takes the provider name as input so the same
    helper can be used to gate other slots in tests.
    """
    name = (provider or "").strip().lower()
    if name == "openai":
        return bool(settings.openai.api_key)
    if name == "openrouter":
        return bool(settings.openrouter.api_key)
    if name == "anthropic":
        return bool(settings.anthropic.api_key)
    if name == "google":
        return bool(settings.google.api_key)
    return name == "local"


@asynccontextmanager
async def _null_async_client():
    """Async no-op replacement for `tg_client(...)`.

    `cmd_ask` always opened a Telegram session even when the user just
    wanted to query the local SQLite archive (no `--chat` / `--folder`).
    Telegram is only actually needed when the user scopes the question
    to a specific chat/folder; for the local-only path we swap in this
    null context so the `async with ... as client` shape stays unchanged
    and the per-flag branches that touch `client` remain correctly
    guarded — they only fire when chat/folder is set anyway, which is
    exactly when the real `tg_client` is opened.
    """
    yield None


def _ask_needs_tg(*, chat: str | None, folder: str | None) -> bool:
    """Decide whether `cmd_ask` needs a live Telegram session.

    Telegram RPCs inside ask are reachable only via the chat- and
    folder-scoped branches: `resolve_ref` (chat), `list_folders`
    (folder), `_refresh_chats` (chat/folder + --refresh), enrichment
    (chat/folder + enabled), and `_mark_as_read` (single-chat scope).
    Every one of these is gated behind chat/folder being set, so a
    single check covers them all. With neither flag, ask reads only
    the local DB and skipping Telegram avoids the session-expired
    dead-end on a YouTube-only / website-only / multi-archive corpus.
    """
    return chat is not None or folder is not None


def _validate_scope_args(
    *,
    ref: str | None,
    chat: str | None,
    folder: str | None,
    global_scope: bool,
) -> None:
    """Reject impossible scope combinations early with a readable message.

    A scope is "set" when its argument is non-None / True. At most one of
    {ref, chat, folder, global} may be set; setting two raises
    BadParameter naming both. None set is fine — caller routes to wizard.
    """
    set_args = []
    if ref is not None:
        set_args.append("ref")
    if chat is not None:
        set_args.append("--chat")
    if folder is not None:
        set_args.append("--folder")
    if global_scope:
        set_args.append("--global")
    if len(set_args) > 1:
        raise typer.BadParameter(f"Cannot combine {' and '.join(set_args)}; pick one scope.")


async def _ask_continue() -> bool:
    """Single-keypress 'Continue chatting?' prompt.

    Bindings: Enter / y → continue (True); n / Esc / Ctrl-C / Ctrl-D →
    exit (False). Default is continue — the user just got an answer
    and Enter should naturally move to the next turn.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("y")
    @kb.add("Y")
    @kb.add("enter")
    def _continue(event):
        event.app.exit(result=True)

    @kb.add("n")
    @kb.add("N")
    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def _exit(event):
        event.app.exit(result=False)

    label = _t("ask_continue_q")
    console.print(f"[bold cyan]{label}[/] [grey70](Enter/y to continue, n/Esc to exit)[/] ", end="")
    session: PromptSession = PromptSession()
    try:
        result = await session.prompt_async("", key_bindings=kb)
    except (EOFError, KeyboardInterrupt):
        result = False
    console.print()  # newline after the keypress
    return bool(result)


async def cmd_ask(
    *,
    question: str | None,
    ref: str | None = None,
    chat: str | None = None,
    thread: int | None = None,
    folder: str | None = None,
    global_scope: bool = False,
    since: str | None = None,
    until: str | None = None,
    last_days: int | None = None,
    last_hours: int | None = None,
    last_minutes: int | None = None,
    limit: int = 200,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    no_console: bool = False,
    no_save: bool = False,
    refresh: bool = False,
    show_retrieved: bool = False,
    rerank: bool | None = None,
    semantic: bool = False,
    build_index: bool = False,
    max_cost: float | None = None,
    no_followup: bool = False,
    with_comments: bool = False,
    enrich: str | None = None,
    enrich_all: bool = False,
    no_enrich: bool = False,
    yes: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
    mark_read: bool | None = None,
) -> None:
    """Ask a free-form question; get a single LLM answer with citations.

    Scoping (mutually exclusive — at most one):
      - positional <ref>: @user / t.me link / topic URL / fuzzy / numeric
      - --chat <ref>: same forms as positional
      - --folder NAME: every chat in the folder
      - --global: every synced chat in the local DB

    Empty question + no scope → opens the wizard (chat picker, period,
    confirm). Empty question + scope set → exits with an error.

    `--refresh` runs an incremental backfill before retrieval; needs
    --chat or --folder.

    After every answer, prompts "Continue chatting?" (default no). Use
    --no-followup to suppress (cron / scripts).

    `mark_read` (tri-state): True advances Telegram's read marker once
    the user exits the conversation; False / None skip. Only meaningful
    when the scope resolves to a single chat — folder / global scopes
    silently no-op since there's no single chat to mark.
    """
    # Bail with a friendly banner if the chat slot's provider has no
    # key — `ask` always ends with an LLM call. Embeddings have a
    # separate gate further down (semantic retrieval degrades to
    # keyword-only when the OpenAI key is missing).
    from unread.ai.providers import _resolve_provider_name as _slot_provider

    _ask_settings = get_settings()
    _chat_slot_provider = _slot_provider(_ask_settings, "chat")
    if not _chat_slot_provider_has_key(_ask_settings, _chat_slot_provider):
        from unread.cli import _print_first_run_banner

        _print_first_run_banner("openai" if _chat_slot_provider == "openai" else "ai")
        raise typer.Exit(1)

    # `tg` is the magic ref token: route to the interactive picker wizard.
    # Treating it before `_validate_scope_args` keeps the wizard reachable
    # even when the user passes other scope flags (the wizard ignores
    # them — picking from the chat list overrides whatever was on the CLI).
    if ref == "tg":
        from unread.interactive import run_interactive_ask

        return await run_interactive_ask(
            question=question or "",
            refresh=refresh,
            semantic=semantic,
            rerank=rerank,
            limit=limit,
            model=model,
            output=output,
            console_out=console_out,
            show_retrieved=show_retrieved,
            max_cost=max_cost,
            yes=yes,
            no_followup=no_followup,
            language=language,
            report_language=report_language,
            source_language=source_language,
            mark_read=mark_read,
        )

    _validate_scope_args(ref=ref, chat=chat, folder=folder, global_scope=global_scope)

    _no_scope = ref is None and chat is None and folder is None and not global_scope

    # --refresh / --build-index require an explicit chat or folder scope;
    # they're incompatible with --global (no chat list to backfill / index).
    if refresh and chat is None and folder is None and ref is None:
        raise typer.BadParameter(
            "--refresh requires <ref>, --chat, or --folder; refusing to backfill every synced dialog."
        )
    if build_index and chat is None and folder is None and ref is None:
        raise typer.BadParameter(
            "--build-index requires <ref>, --chat, or --folder; "
            "refusing to embed every synced dialog at once."
        )

    if _no_scope:
        # No scope, no ref, no `tg` token → refuse to guess. Pre-fix
        # this fell through to a wizard that opened a Telegram client
        # and surprised users with a session prompt for a command they
        # thought was a quick question against the local archive.
        # Direct them at `tg` (the picker) or `--global` (local archive).
        raise typer.BadParameter(
            "Need a ref or scope. Use `tg` for the interactive chat picker, "
            "an @user / t.me link / numeric id for one chat, "
            "`--folder NAME` for a folder, or `--global` to query every synced chat in the local DB."
        )

    # Scope is set; an empty question here is a user error.
    if question is None or not question.strip():
        console.print(f"[red]{_t('ask_empty_question')}[/]")
        raise typer.Exit(2)

    # If ref is set, resolve it now and overlay onto chat/thread.
    if ref is not None:
        _settings_for_ref = get_settings()
        async with (
            tg_client(_settings_for_ref) as _client_for_ref,
            open_repo(_settings_for_ref.storage.data_path) as _repo_for_ref,
        ):
            # msg_id from URL is intentionally discarded; ask scopes to chat/topic,
            # not a single message.
            _chat_id, _ref_thread_id, _ = await _resolve_ask_ref(_client_for_ref, _repo_for_ref, ref)
        chat = str(_chat_id)
        if thread is None and _ref_thread_id is not None:
            thread = _ref_thread_id

    # `--build-index` doesn't need a question; everything else does.
    if not build_index and not semantic:
        tokens = tokenize_question(question)
        if not tokens:
            console.print(
                "[yellow]No useful keywords in your question.[/] Add a noun, name, or "
                "topic — stop words and short tokens are filtered. (Or pass --semantic, "
                "which doesn't need keyword tokens.)"
            )
            raise typer.Exit(2)
    settings = get_settings()
    effective_language = (language or settings.locale.language or "en").lower()
    effective_report_language = (
        report_language or settings.locale.report_language or effective_language
    ).lower()
    # Whisper-style source-content hint. Empty = LLM auto-detects from
    # the cited messages. CLI flag wins over saved settings.
    effective_source_language = (
        (source_language if source_language is not None else settings.locale.content_language).strip().lower()
    )
    since_dt, until_dt = compute_window(since, until, last_days, last_hours, last_minutes)

    # Skip the Telegram open when the request is fully local — see
    # `_ask_needs_tg` for the rule. Without this guard, every `unread
    # ask "..."` against the local archive triggered a session check
    # and died on the "session expired" banner even though no Telegram
    # RPC would actually have been issued.
    _client_cm = tg_client(settings) if _ask_needs_tg(chat=chat, folder=folder) else _null_async_client()
    async with _client_cm as client, open_repo(settings.storage.data_path) as repo:
        # Resolve scope.
        chat_ids: list[int] | None = None
        chat_titles: dict[int, str] = {}
        if chat:
            console.print(f"[grey70]{_tf('resolving', ref=chat)}[/]")
            resolved = await resolve_ref(client, repo, chat)
            chat_ids = [resolved.chat_id]
            chat_titles[resolved.chat_id] = resolved.title or str(resolved.chat_id)
            # `--with-comments` on a channel: add the linked discussion
            # group to scope so retrieval (keyword / semantic) sees both.
            # Falls back gracefully when not a channel or no linked chat.
            if with_comments:
                from unread.tg.topics import get_linked_chat_id

                row = await repo.get_chat(resolved.chat_id)
                linked_id = (row or {}).get("linked_chat_id")
                linked_lookup_err: str | None = None
                if linked_id is None and (row or {}).get("kind") == "channel":
                    try:
                        linked_id = await get_linked_chat_id(client, resolved.chat_id)
                    except Exception as e:
                        linked_id = None
                        linked_lookup_err = f"{type(e).__name__}: {str(e)[:120]}"
                        # Don't bury this — the user explicitly asked for
                        # comments scope and is otherwise about to get a
                        # generic "no linked discussion group" message
                        # that hides a real failure (network blip, perms).
                        log.warning(
                            "ask.linked_chat_resolve_failed",
                            chat_id=resolved.chat_id,
                            err=str(e)[:200],
                        )
                        console.print(
                            f"[yellow]→ --with-comments: couldn't resolve linked discussion "
                            f"group ({linked_lookup_err}); proceeding without comments.[/]"
                        )
                    if linked_id is not None:
                        await repo.upsert_chat(
                            resolved.chat_id,
                            "channel",
                            title=resolved.title,
                            username=resolved.username,
                            linked_chat_id=linked_id,
                        )
                if linked_id is not None:
                    chat_ids.append(linked_id)
                    linked_row = await repo.get_chat(linked_id) or {}
                    chat_titles[linked_id] = linked_row.get("title") or f"Comments {linked_id}"
                    console.print(
                        f"[grey70]→ Including comments from linked chat[/] "
                        f"[bold]{chat_titles[linked_id]}[/] ({linked_id})"
                    )
                elif linked_lookup_err is None:
                    # Only print the "no linked group" message when the
                    # lookup actually succeeded with a None result — the
                    # error path above already printed its own message.
                    console.print(
                        "[yellow]→ --with-comments: chat is not a channel "
                        "or has no linked discussion group; ignoring.[/]"
                    )
        elif folder:
            folders = await list_folders(client)
            matched = resolve_folder(folder, folders)
            if matched is None:
                titles = ", ".join(f"'{f.title}'" for f in folders) or "(none)"
                console.print(f"[red]{_tf('no_folder_matching', folder=folder, titles=titles)}[/]")
                raise typer.Exit(2)
            chat_ids = list(matched.include_chat_ids)
            if not chat_ids:
                console.print(
                    f"[yellow]Folder '{matched.title}' has no explicitly-listed chats[/] "
                    "(rule-based folders aren't expanded)."
                )
                raise typer.Exit(2)
            console.print(
                f"[grey70]{_t('ask_folder_label')}[/] [bold]{matched.title}[/] — "
                f"{_tf('ask_n_chats', n=len(chat_ids))}"
            )
        # else: chat_ids stays None → search all synced chats.

        if refresh and chat_ids:
            await _refresh_chats(client, repo, chat_ids, thread_id=thread, since_date=since_dt)

        # Run media enrichment over the scoped chats + period BEFORE
        # retrieval, so transcripts / image descriptions / link summaries
        # become searchable in this run. Mirrors `analyze`'s enrich flags
        # (--enrich / --enrich-all / --no-enrich); shares the same
        # `EnrichOpts` builder + `enrich_messages` orchestrator that
        # `core/pipeline.prepare_chat_run` uses. ask deliberately keeps
        # its own pipeline (per CLAUDE.md invariant 9) so this is a thin
        # call into the shared helper, not a re-implementation.
        if chat_ids:
            from unread.analyzer.commands import build_enrich_opts as _build_enrich_opts
            from unread.enrich.pipeline import enrich_messages

            enrich_opts = _build_enrich_opts(
                cli_enrich=enrich,
                cli_enrich_all=enrich_all,
                cli_no_enrich=no_enrich,
                preset=None,  # ask has no preset → no preset-declared kinds
            )
            if enrich_opts.any_enabled():
                msgs_to_enrich = []
                for cid in chat_ids:
                    async for m in repo.iter_messages(
                        cid,
                        thread_id=thread,
                        since=since_dt,
                        until=until_dt,
                    ):
                        msgs_to_enrich.append(m)
                if msgs_to_enrich:
                    console.print(
                        f"[grey70]→ Enriching {len(msgs_to_enrich)} messages "
                        f"({', '.join(enrich_opts.kinds_enabled())})...[/]"
                    )
                    stats = await enrich_messages(
                        msgs_to_enrich,
                        client=client,
                        repo=repo,
                        opts=enrich_opts,
                        language=effective_language,
                        report_language=effective_report_language,
                        source_language=effective_source_language,
                    )
                    summary = stats.summary()
                    if summary:
                        console.print(f"[grey70]→ {summary}[/]")

        # --build-index → fill the message_embeddings table for the scoped
        # chats and exit. Idempotent. The flagship answer path is skipped.
        if build_index:
            from openai import AsyncOpenAI

            from unread.ask.embeddings import build_index as _build_index
            from unread.ask.embeddings import default_model as _default_embed_model

            # Embeddings are OpenAI-only — build a direct AsyncOpenAI
            # client with the OpenAI key, regardless of which chat
            # provider is active. Bail with a friendly message when the
            # key is missing.
            if not settings.openai.api_key:
                console.print(
                    "[yellow]Embeddings (`ask --semantic --build-index`) need an OpenAI key.[/] "
                    "Run `unread init` and add one (chat provider can stay non-OpenAI)."
                )
                raise typer.Exit(1)

            assert chat_ids is not None  # validated above
            embed_model = _default_embed_model()
            console.print(
                f"[grey70]→ Building embeddings index for {len(chat_ids)} chat(s) "
                f"with[/] [bold]{embed_model}[/]..."
            )
            embed_client = AsyncOpenAI(
                api_key=settings.openai.api_key,
                timeout=settings.openai.request_timeout_sec,
            )
            written = await _build_index(
                repo=repo,
                oai=embed_client,
                chat_ids=chat_ids,
                model=embed_model,
                # Without --yes, surface a cost heads-up for big backfills
                # (>5k messages) so a power user with hundreds of synced
                # chats doesn't incur a surprise bill on first run.
                confirm=not yes,
            )
            if written:
                console.print(f"[green]{_tf('ask_indexed_n', n=written)}[/]")
            else:
                console.print(f"[grey70]{_t('ask_index_up_to_date')}[/]")
            return

        # Rerank decision: explicit CLI flag wins, else config default.
        ask_cfg = settings.ask
        rerank_on = ask_cfg.rerank_enabled if rerank is None else rerank
        # Rerank composes with semantic (semantic produces the pool, rerank
        # prunes it). For pure semantic without rerank, skip rerank.
        oai = make_client()
        used_model = model or settings.openai.chat_model_default
        # Conversation history for --interactive mode. Each turn appends
        # (question, answer) so follow-ups have prior context.
        history: list[tuple[str, str]] = []
        # Last successful turn's retrieved pool — used as fallback for
        # short / conversational follow-ups ("привет", "tell me more")
        # whose own retrieval matches nothing. Without this, the loop
        # bails on every greeting and the user can't have a real
        # conversation.
        prior_pool: list[tuple] = []

        async def _answer_one(q: str, *, is_followup: bool) -> tuple[str, list[tuple]]:
            """One full Q→A iteration: retrieve → rerank → format → answer.

            Returns `(answer_text, scored_pool_used)`. The pool is what
            the LLM actually saw; the caller stashes it as `prior_pool`
            for the next follow-up.
            """
            return await _run_single_turn(
                question=q,
                history=history,
                client=client,
                repo=repo,
                settings=settings,
                ask_cfg=ask_cfg,
                oai=oai,
                used_model=used_model,
                chat_ids=chat_ids,
                chat_titles=chat_titles,
                thread=thread,
                folder=folder,
                since_dt=since_dt,
                until_dt=until_dt,
                limit=limit,
                rerank_on=rerank_on,
                semantic=semantic,
                show_retrieved=show_retrieved,
                output=output if not is_followup else None,
                console_out=console_out or is_followup,
                no_console=no_console if not is_followup else False,
                no_save=no_save if not is_followup else True,
                max_cost=max_cost,
                yes=yes,
                fallback_pool=prior_pool if is_followup else None,
                language=effective_language,
                report_language=effective_report_language,
                source_language=effective_source_language,
            )

        # First turn — same shape as before --interactive existed.
        first_answer, prior_pool = await _answer_one(question, is_followup=False)
        history.append((question, first_answer))

        async def _maybe_mark_read() -> None:
            """Advance Telegram's read marker for the single-chat scope.

            Only meaningful when `chat_ids` resolves to exactly one chat —
            multi-chat folder scope and global scope have nothing single
            to mark, so this is a silent no-op there. Failures log + warn
            but never abort the answer (the report is already on screen
            / on disk).
            """
            if not mark_read or not chat_ids or len(chat_ids) != 1:
                return
            try:
                target_chat = chat_ids[0]
                # Pre-prod review: previously preferred max(prior_pool.msg_id)
                # which depends on retrieval scoring — re-running the same
                # question with a slightly different `--limit` advanced
                # the marker to a different msg_id, so two consecutive
                # `unread ask` runs marked different ranges as read.
                # Use `repo.get_max_msg_id` deterministically: the
                # marker advances to "everything we currently have
                # locally," which matches the user's mental model
                # ("I've now reviewed this chat") regardless of which
                # subset retrieval surfaced.
                max_id = await repo.get_max_msg_id(target_chat, thread_id=thread)
                if not max_id:
                    return
                from unread.tg.dialogs import mark_as_read as _mark_as_read

                ok = await _mark_as_read(client, target_chat, int(max_id), thread_id=thread)
                if ok:
                    console.print(f"[grey70]{_tf('marked_read_up_to', msg_id=max_id)}[/]")
            except Exception as e:
                log.warning("ask.mark_read_failed", chat_id=chat_ids[0], err=str(e)[:200])
                console.print(f"[yellow]{_tf('couldnt_mark_read', err=e)}[/]")

        if no_followup:
            await _maybe_mark_read()
            return

        # Post-answer prompt — Enter or `y` continues, `n` or Esc exits.
        # Default = continue (the user just got an answer; the cheap path
        # is "press Enter for more").
        if not await _ask_continue():
            await _maybe_mark_read()
            return

        # User opted in → drop into the multi-turn follow-up loop.
        # Plain `input()` misbehaves inside the asyncio event loop on
        # macOS — Enter shows up as a literal `^M` (raw-mode carriage
        # return) and the line never submits. prompt_toolkit's async
        # session correctly hands stdin back and forth with the loop AND
        # supports Cyrillic / non-ASCII typing out of the box.
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings

        console.print(
            "\n[bold cyan]Interactive mode[/] — type a follow-up question (Esc / blank / Ctrl-D to exit)."
        )
        # Esc inside the loop's input → submit empty → break (consistent
        # with blank Enter). Without this binding, prompt_toolkit's
        # default Esc starts an emacs meta-key prefix and doesn't exit.
        loop_kb = KeyBindings()

        @loop_kb.add("escape", eager=True)
        def _(event):
            event.app.exit(result="")

        prompt_session: PromptSession = PromptSession(key_bindings=loop_kb)

        while True:
            try:
                follow = (await prompt_session.prompt_async(HTML("\n<ansicyan>> </ansicyan>"))).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not follow:
                break
            try:
                ans, prior_pool = await _answer_one(follow, is_followup=True)
            except typer.Exit as e:
                # Budget guard fires raise; in interactive mode that means
                # "skip this turn", not "kill the session".
                if e.exit_code == 0:
                    continue
                raise
            history.append((follow, ans))

        # User exited the follow-up loop (blank Enter / Esc / Ctrl-D /
        # Ctrl-C). Mark read using the latest pool so any messages cited
        # across follow-ups also count toward the read marker.
        await _maybe_mark_read()


async def _run_single_turn(
    *,
    question: str,
    history: list[tuple[str, str]],
    client,
    repo,
    settings,
    ask_cfg,
    oai,
    used_model: str,
    chat_ids: list[int] | None,
    chat_titles: dict[int, str],
    thread: int | None,
    folder: str | None,
    since_dt,
    until_dt,
    limit: int,
    rerank_on: bool,
    semantic: bool,
    show_retrieved: bool,
    output,
    console_out: bool,
    no_console: bool = False,
    no_save: bool = False,
    max_cost: float | None,
    yes: bool,
    fallback_pool: list[tuple] | None = None,
    language: str = "en",
    report_language: str = "en",
    source_language: str = "",
) -> tuple[str, list[tuple]]:
    """Retrieve → rerank → format → preview → answer for one question.

    Returns `(answer_text, scored_pool_used)`. The pool is the list of
    `(Message, score)` tuples the LLM actually saw — caller stashes it
    so the next conversational follow-up can fall back on it.

    Raises typer.Exit(0) on retrieval miss with no fallback, or budget
    abort. Exit(2) on --yes-driven over-budget abort. Exit(1) on empty
    model output. Prints / saves the answer using the same UX as the
    original single-shot path.
    """
    from unread.core.paths import derive_internal_id
    from unread.util.pricing import chat_cost
    from unread.util.tokens import count_tokens

    tokens = tokenize_question(question)
    candidate_limit = max(limit, ask_cfg.rerank_top_k) if rerank_on else limit

    # Embeddings are OpenAI-only. When `--semantic` was requested but
    # the OpenAI key is missing, degrade to keyword retrieval with a
    # one-line warning — the answer still goes through whichever
    # chat-slot provider the user configured (Anthropic, Gemini, …).
    if semantic and not settings.openai.api_key:
        console.print(
            "[yellow]No OpenAI key — embeddings disabled. "
            "Falling back to keyword retrieval; results may be less precise.[/]"
        )
        semantic = False

    if semantic:
        from openai import AsyncOpenAI

        from unread.ask.embeddings import default_model as _embed_model_name
        from unread.ask.embeddings import semantic_search

        embed_model = _embed_model_name()
        if chat_ids is None:
            console.print(
                "[red]--semantic needs --chat or --folder.[/] Cosine over every "
                "synced chat would be slow without an ANN index."
            )
            raise typer.Exit(2)
        console.print(
            f"[grey70]→ Semantic retrieval[/] ({embed_model}; "
            f"pool={candidate_limit}"
            f"{', rerank→' + str(min(limit, ask_cfg.rerank_keep)) if rerank_on else ''})"
        )
        embed_client = AsyncOpenAI(
            api_key=settings.openai.api_key,
            timeout=settings.openai.request_timeout_sec,
        )
        sem_scored = await semantic_search(
            repo=repo,
            oai=embed_client,
            question=question,
            chat_ids=chat_ids,
            model=embed_model,
            limit=candidate_limit,
        )
        if not sem_scored:
            console.print(
                "[yellow]No embeddings indexed for this scope.[/] "
                "Run `unread ask --build-index --chat <ref>` (or `--folder`) first."
            )
            raise typer.Exit(0)
        # Map cosine [-1,1] → [0,100] int for the (Message, score) tuple
        # shape the rest of the pipeline expects from keyword retrieval.
        # Affine transform `(s + 1) * 50` keeps the score non-negative so
        # rerank's "missing rating defaults to 0" sorting trick still works.
        # Negative cosines (semantically opposite) map to [0, 50);
        # neutral → 50; closely related → (50, 100].
        scored = [(m, max(0, min(100, round((s + 1) * 50)))) for m, s in sem_scored]
    else:
        console.print(
            f"[grey70]→ Searching local corpus[/] (tokens: {', '.join(tokens) or '(none)'}; "
            f"pool={candidate_limit}"
            f"{', rerank→' + str(min(limit, ask_cfg.rerank_keep)) if rerank_on else ''})"
        )
        scored = await retrieve_messages(
            repo=repo,
            question=question,
            chat_ids=chat_ids,
            thread_id=thread,
            since=since_dt,
            until=until_dt,
            limit=candidate_limit,
            return_scores=True,
        )

    if rerank_on and len(scored) > min(limit, ask_cfg.rerank_keep):
        from unread.ask.rerank import rerank as _rerank_fn

        keep_n = min(limit, ask_cfg.rerank_keep)
        rerank_model = ask_cfg.rerank_model or settings.openai.filter_model_default
        console.print(
            f"[grey70]→ Reranking {len(scored)} candidates with[/] [bold]{rerank_model}[/]"
            f"[grey70] → keep top-{keep_n}...[/]"
        )
        scored = await _rerank_fn(
            repo=repo,
            pool=scored,
            question=question,
            model=rerank_model,
            keep=keep_n,
            batch_size=ask_cfg.rerank_batch_size,
        )
        scored.sort(key=lambda p: (p[0].chat_id, p[0].date or datetime.min, p[0].msg_id))

    msgs = [m for m, _ in scored]
    if not msgs:
        # Conversational follow-ups ("привет", "tell me more") rarely
        # have content tokens that match anything new. Reuse the prior
        # turn's pool so the LLM can keep the thread instead of dying
        # on every greeting.
        if fallback_pool:
            scored = list(fallback_pool)
            msgs = [m for m, _ in scored]
            console.print(f"[grey70]{_t('ask_no_matches_reusing')}[/]")
        else:
            console.print(
                "[yellow]No matching messages.[/] Try `unread sync <chat>` first if "
                "the chat hasn't been backfilled, or broaden your scope."
            )
            raise typer.Exit(0)

    # Title backfill for cross-chat answers.
    for m in msgs:
        if m.chat_id in chat_titles:
            continue
        row = await repo.get_chat(m.chat_id)
        chat_titles[m.chat_id] = (row or {}).get("title") or str(m.chat_id)

    if show_retrieved:
        _print_retrieved_table(scored, chat_titles)

    chat_links: dict[int, str | None] = {}
    for cid in {m.chat_id for m in msgs}:
        row = await repo.get_chat(cid)
        chat_links[cid] = build_link_template(
            chat_username=(row or {}).get("username"),
            chat_internal_id=derive_internal_id(cid),
            thread_id=thread,
        )

    # `report_language` drives LLM-facing strings: system prompt,
    # user-template labels (Question:/Context:/Answer:), chat-group
    # header `=== Chat: ... ===`, and the formatter labels in the
    # context block. `language` is used only for what the user sees
    # rendered by `unread` (cost preview, status messages — already English
    # in the source). `source_language`, when set, is appended below as a
    # one-line Whisper-style hint so the LLM trusts the input is in that
    # language without trying to infer it.
    llm_lang = report_language
    if chat_ids is not None and len(chat_ids) == 1:
        single_chat_id = chat_ids[0]
        formatted = format_messages(msgs, link_template=chat_links.get(single_chat_id), language=llm_lang)
        scope_label = chat_titles[single_chat_id]
    else:
        formatted = _format_multi_chat(msgs, chat_titles, chat_links, language=llm_lang)
        scope_label = f"folder '{folder}'" if folder else "all synced chats"

    user_text = (
        f"{_t('ask_question', llm_lang)}: {question.strip()}\n\n"
        f"{_t('ask_context', llm_lang)} ({len(msgs)} {_t('ask_msgs_short', llm_lang)} "
        f"{_t('ask_from_scope', llm_lang)} {scope_label}):\n\n"
        f"{formatted}\n\n"
        f"{_t('ask_answer_with_citations', llm_lang)}"
    )

    system_prompt = _resolve_system_prompt(llm_lang)
    if source_language:
        system_prompt = (
            f"{system_prompt}\n\n"
            f"The source content is in {source_language}. "
            "Treat citations and quotations as that language; do not translate them."
        )
    # Cost preview against the *full* messages list (system + history + user).
    messages = _build_history_messages(system_prompt, history, user_text)
    prompt_tokens = sum(count_tokens(m["content"], used_model) for m in messages)
    est_cost = chat_cost(used_model, prompt_tokens, 0, 2000, settings=settings)
    if est_cost is not None:
        console.print(
            f"[grey70]→ Estimated cost: ~${est_cost:.4f}[/] "
            f"({prompt_tokens:,} prompt tokens × {used_model}; output capped at 2000)"
        )
        if max_cost is not None and est_cost > max_cost:
            console.print(
                "[bold yellow]"
                + _tf("max_cost_exceeded", lo=est_cost, hi=est_cost, max=max_cost, n=len(msgs), preset="ask")
                + "[/]"
            )
            if yes:
                console.print(f"[red]{_t('aborting_yes_set')}[/]")
                raise typer.Exit(2)
            from unread.util.prompt import confirm as _confirm

            if not _confirm(_t("run_anyway_q"), default=False):
                console.print(f"[yellow]{_t('aborted')}[/]")
                raise typer.Exit(0)
    elif max_cost is not None:
        console.print(f"[grey70]{_t('max_cost_not_enforced')}[/]")

    console.print(
        f"[grey70]{_t('ask_asking_label')}[/] [bold]{used_model}[/] {_tf('ask_over_n_msgs', n=len(msgs))}"
    )
    res = await chat_complete(
        oai,
        repo=repo,
        model=used_model,
        messages=messages,
        max_tokens=2000,
        context={
            "phase": "ask",
            "scope": scope_label,
            "tokens": tokens[:10],
            "turn": len(history) + 1,
        },
    )
    answer = (res.text or "").strip()
    if not answer:
        console.print(f"[red]{_t('ask_model_empty')}[/]")
        raise typer.Exit(1)

    is_first_turn = len(history) == 0
    if is_first_turn:
        # First turn: render the full analyze-style shell via the shared
        # report-render helper — same `Run …` summary line + Rule + grid +
        # body shape as `unread <ref>` (analyze).
        from datetime import datetime

        from unread.analyzer.commands import _fmt_cost_precise
        from unread.ask.render import default_ask_path, truncate_value
        from unread.i18n import tf as _tf2
        from unread.util.report_render import print_report_shell

        if semantic:
            mode_value = _tf2("ask_mode_semantic", pool=limit)
        else:
            mode_value = _tf2("ask_mode_keyword", pool=candidate_limit)
        if rerank_on:
            keep_n = min(limit, ask_cfg.rerank_keep)
            mode_value += _tf2("ask_mode_rerank_suffix", keep=keep_n)

        header_rows: list[tuple[str, str]] = [
            (_t("ask_meta_scope"), scope_label),
            (_t("ask_meta_question"), truncate_value(question, 120)),
            (_t("ask_meta_mode"), mode_value),
            (_t("ask_meta_messages"), _tf2("ask_messages_retrieved", n=len(msgs))),
            (_t("report_meta_model"), f"`{used_model}`"),
            (_t("report_meta_cost"), _fmt_cost_precise(float(res.cost_usd or 0))),
            (_t("report_meta_generated"), datetime.now().strftime("%Y-%m-%d %H:%M")),
        ]

        # Legacy behavior shim: --output without --console used to mean
        # "save only, no terminal render". Preserve it so existing
        # scripts keep working when callers haven't migrated to
        # --no-console / --no-save.
        effective_no_console = no_console or (output is not None and not console_out and not no_save)

        summary_line = (
            f"[bold cyan]{_t('report_summary_run')}[/] scope={scope_label} "
            f"mode={mode_value} messages={len(msgs)} cost=${float(res.cost_usd or 0):.4f}"
        )

        print_report_shell(
            summary_line=summary_line,
            title=scope_label,
            meta_rows=header_rows,
            body_md=f"{answer}\n",
            output=output,
            default_path=default_ask_path("tg", scope_label),
            no_console=effective_no_console,
            no_save=no_save,
            plain_citations=get_settings().analyze.plain_citations,
        )
    else:
        # Follow-up turn inside the conversational loop: keep a slim
        # Rule + Markdown render so the screen doesn't fill with the
        # full header on every reply. Don't save follow-ups — the first
        # turn's report carries the canonical scope record; chasing
        # every interactive turn into a separate file would just
        # produce spam under reports/ask/tg/.
        from rich.markdown import Markdown
        from rich.rule import Rule

        from unread.analyzer.commands import _flatten_citations

        console_body = f"{answer}\n"
        if get_settings().analyze.plain_citations:
            console_body = _flatten_citations(console_body)
        console.print(f"[grey70]turn {len(history) + 1} · {used_model} · ${float(res.cost_usd or 0):.4f}[/]")
        console.print(Rule("answer", style="cyan"))
        console.print(Markdown(console_body))
        console.print(Rule(style="cyan"))
    return answer, scored


def _build_history_messages(
    system: str,
    history: list[tuple[str, str]],
    new_user_text: str,
) -> list[dict[str, str]]:
    """Build a multi-turn messages list: system → (user, assistant)*N → user.

    Empty `history` falls back to the standard two-message shape so prompt
    caching still hits on the first turn (system prefix is byte-identical
    to the single-shot path).
    """
    if not history:
        return [{"role": "system", "content": system}, {"role": "user", "content": new_user_text}]
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    for q, a in history:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": new_user_text})
    return msgs


def _print_retrieved_table(
    scored: list[tuple],
    chat_titles: dict[int, str],
) -> None:
    """Render the retrieval result as a Rich Table.

    One row per retrieved message: relevance score, chat, date, msg_id,
    short text excerpt. Sorted score-desc so the LLM-relevant rows are
    on top. Sole purpose is debug visibility — `--show-retrieved` is the
    fastest answer to "why did the LLM cite #11537?"
    """
    from rich.table import Table

    by_score = sorted(scored, key=lambda p: (-p[1], p[0].date or datetime.min))
    t = Table(title=f"Retrieved {len(scored)} message(s)", show_lines=False)
    t.add_column("score", justify="right")
    t.add_column("chat")
    t.add_column("date")
    t.add_column("msg_id", justify="right")
    t.add_column("excerpt")
    for m, score in by_score:
        body = (m.text or m.transcript or "").strip().replace("\n", " ")
        if len(body) > 80:
            body = body[:77] + "…"
        date_s = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "—"
        t.add_row(
            str(score),
            chat_titles.get(m.chat_id, str(m.chat_id))[:30],
            date_s,
            str(m.msg_id),
            body,
        )
    console.print(t)


async def _refresh_chats(
    client,
    repo,
    chat_ids: list[int],
    *,
    thread_id: int | None = None,
    since_date: datetime | None = None,
) -> None:
    """Forward-direction backfill bounded by the user's time window.

    Walks per chat in parallel (capped at 3 concurrent backfills to stay
    friendly to Telegram). Per-chat failures log a warning but don't
    abort the rest of the refresh — `ask` can still answer over
    whatever's already synced.

    Bounding rules:
      - `since_date` set + chat has local data: forward-walk from
        `local_max` (only NEW messages since the last sync are fetched —
        cheap incremental).
      - `since_date` set + chat is empty locally: forward-walk from
        `since_date` (bounded to the user's window — avoids pulling the
        entire chat history on first --refresh).
      - `since_date` not set + chat has local data: forward-walk from
        `local_max` (existing incremental behavior).
      - `since_date` not set + chat is empty locally: full history
        (Telethon default, used when the user explicitly asked for no
        time filter).
    """
    import asyncio as _asyncio

    from unread.tg.sync import backfill

    sem = _asyncio.Semaphore(3)
    console.print(f"[grey70]{_tf('ask_refreshing', n=len(chat_ids))}[/]")

    async def _one(chat_id: int) -> tuple[int, int | None, str | None]:
        async with sem:
            try:
                local_max = await repo.get_max_msg_id(chat_id, thread_id=thread_id)
                kwargs: dict = {
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "direction": "forward",
                }
                # Pass both bounds when available. Telethon's
                # `iter_messages(min_id=..., offset_date=..., reverse=True)`
                # is unreliable: `min_id` dominates and the date bound is
                # silently dropped (see commit 466bf69). Until that's
                # fixed upstream, backfill on a forward walk uses
                # `offset_date` ALONE when `since_date` is set — at the
                # cost of re-fetching messages older than `local_max`
                # but younger than `since_date` (idempotent in the DB).
                # Future work: pick the tighter bound at this call site
                # by looking up `local_max`'s date and comparing.
                if local_max:
                    kwargs["from_msg_id"] = local_max
                if since_date is not None:
                    kwargs["since_date"] = since_date
                # When neither is set: full-history walk (Telethon default).
                fetched = await backfill(client, repo, **kwargs)
                return chat_id, fetched, None
            except Exception as e:
                log.warning("ask.refresh_failed", chat_id=chat_id, err=str(e)[:200])
                return chat_id, None, str(e)[:200]

    results = await _asyncio.gather(*(_one(cid) for cid in chat_ids))
    total_new = sum(n for _, n, _ in results if n)
    failed = [(cid, err) for cid, n, err in results if err]
    if total_new:
        console.print(f"[green]{_tf('ask_refreshed_total', total=total_new, n=len(chat_ids))}[/]")
    else:
        console.print(f"[grey70]{_t('ask_refreshed_none')}[/]")
    if failed:
        console.print(f"[yellow]{_tf('ask_refresh_failed', n=len(failed))}[/]")


def _format_multi_chat(
    msgs,
    chat_titles: dict[int, str],
    chat_links: dict[int, str | None],
    *,
    language: str = "en",
) -> str:
    """Group messages by chat and render with a chat-title separator.

    Mirrors the topic-grouped format the analyzer uses for flat-forum
    runs — keeps each chat's conversation contiguous so the LLM can
    answer cross-chat questions without losing thread. Each group gets
    its own message-link template so citations like
    `[#11537](https://t.me/...)` resolve correctly regardless of which
    chat the msg_id came from.
    """
    from itertools import groupby

    chat_lbl = _t("chat_label", language)
    chunks: list[str] = []
    msgs_sorted = sorted(msgs, key=lambda m: (m.chat_id, m.date or datetime.min, m.msg_id))
    for chat_id, group in groupby(msgs_sorted, key=lambda m: m.chat_id):
        title = chat_titles.get(chat_id, str(chat_id))
        chunks.append(f"=== {chat_lbl}: {title} (id={chat_id}) ===")
        chunks.append(format_messages(list(group), link_template=chat_links.get(chat_id), language=language))
    return "\n\n".join(chunks)
