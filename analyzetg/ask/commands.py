"""`atg ask` command — answer a question over the local Telegram corpus.

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

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.analyzer.formatter import build_link_template, format_messages
from analyzetg.analyzer.openai_client import chat_complete, make_client
from analyzetg.ask.retrieval import retrieve_messages, tokenize_question
from analyzetg.config import get_settings
from analyzetg.core.paths import compute_window
from analyzetg.db.repo import open_repo
from analyzetg.i18n import t as _t
from analyzetg.i18n import tf as _tf
from analyzetg.tg.client import tg_client
from analyzetg.tg.folders import list_folders, resolve_folder
from analyzetg.tg.resolver import resolve as resolve_ref
from analyzetg.util.logging import get_logger

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
    limit: int = 200,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    refresh: bool = False,
    show_retrieved: bool = False,
    rerank: bool | None = None,
    semantic: bool = False,
    build_index: bool = False,
    max_cost: float | None = None,
    no_followup: bool = False,
    with_comments: bool = False,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
) -> None:
    """Ask a free-form question; get a single LLM answer with citations.

    `--chat` / `--thread` / `--folder` narrow the search corpus. With none
    of those, every synced message in the local DB is eligible.

    `--refresh` runs an incremental backfill on the scoped chat(s) before
    retrieval — useful when you suspect new messages have arrived since
    the last `analyze` / `dump` / `sync`. Requires `--chat` or `--folder`
    so we don't accidentally hit Telegram for every dialog you've ever
    synced.
    """
    _validate_scope_args(ref=ref, chat=chat, folder=folder, global_scope=global_scope)

    # No question → drop into the wizard (which prompts inline). If user
    # passed a scope arg without a question, error out.
    if question is None or not question.strip():
        if ref is None and chat is None and folder is None and not global_scope:
            from analyzetg.interactive import run_interactive_ask

            return await run_interactive_ask(
                question="",  # wizard prompts for it
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
                content_language=content_language,
            )
        console.print(f"[red]{_t('ask_empty_question')}[/]")
        raise typer.Exit(2)

    # If ref is set, resolve it now and overlay onto chat/thread.
    if ref is not None:
        _settings_for_ref = get_settings()
        async with (
            tg_client(_settings_for_ref) as _client_for_ref,
            open_repo(_settings_for_ref.storage.data_path) as _repo_for_ref,
        ):
            _chat_id, _ref_thread_id, _msg_id = await _resolve_ask_ref(_client_for_ref, _repo_for_ref, ref)
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
    if refresh and not chat and not folder:
        raise typer.BadParameter(
            "--refresh needs --chat or --folder; refusing to backfill every "
            "synced dialog (potentially hundreds of Telegram round-trips)."
        )
    if build_index and not chat and not folder:
        raise typer.BadParameter(
            "--build-index needs --chat or --folder; refusing to embed every "
            "synced dialog at once (could be a lot of OpenAI calls)."
        )

    settings = get_settings()
    effective_language = (language or settings.locale.language or "en").lower()
    effective_content_language = (
        content_language or settings.locale.content_language or effective_language
    ).lower()
    since_dt, until_dt = compute_window(since, until, last_days)

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        # Resolve scope.
        chat_ids: list[int] | None = None
        chat_titles: dict[int, str] = {}
        if chat:
            console.print(f"[dim]{_tf('resolving', ref=chat)}[/]")
            resolved = await resolve_ref(client, repo, chat)
            chat_ids = [resolved.chat_id]
            chat_titles[resolved.chat_id] = resolved.title or str(resolved.chat_id)
            # `--with-comments` on a channel: add the linked discussion
            # group to scope so retrieval (keyword / semantic) sees both.
            # Falls back gracefully when not a channel or no linked chat.
            if with_comments:
                from analyzetg.tg.topics import get_linked_chat_id

                row = await repo.get_chat(resolved.chat_id)
                linked_id = (row or {}).get("linked_chat_id")
                if linked_id is None and (row or {}).get("kind") == "channel":
                    try:
                        linked_id = await get_linked_chat_id(client, resolved.chat_id)
                    except Exception:
                        linked_id = None
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
                        f"[dim]→ Including comments from linked chat[/] "
                        f"[bold]{chat_titles[linked_id]}[/] ({linked_id})"
                    )
                else:
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
                f"[dim]{_t('ask_folder_label')}[/] [bold]{matched.title}[/] — "
                f"{_tf('ask_n_chats', n=len(chat_ids))}"
            )
        # else: chat_ids stays None → search all synced chats.

        if refresh and chat_ids:
            await _refresh_chats(client, repo, chat_ids, thread_id=thread)

        # --build-index → fill the message_embeddings table for the scoped
        # chats and exit. Idempotent. The flagship answer path is skipped.
        if build_index:
            from analyzetg.ask.embeddings import build_index as _build_index
            from analyzetg.ask.embeddings import default_model as _default_embed_model

            assert chat_ids is not None  # validated above
            embed_model = _default_embed_model()
            console.print(
                f"[dim]→ Building embeddings index for {len(chat_ids)} chat(s) "
                f"with[/] [bold]{embed_model}[/]..."
            )
            written = await _build_index(
                repo=repo,
                oai=make_client(),
                chat_ids=chat_ids,
                model=embed_model,
            )
            if written:
                console.print(f"[green]{_tf('ask_indexed_n', n=written)}[/]")
            else:
                console.print(f"[dim]{_t('ask_index_up_to_date')}[/]")
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
                max_cost=max_cost,
                yes=yes,
                fallback_pool=prior_pool if is_followup else None,
                language=effective_language,
                content_language=effective_content_language,
            )

        # First turn — same shape as before --interactive existed.
        first_answer, prior_pool = await _answer_one(question, is_followup=False)
        history.append((question, first_answer))

        if no_followup:
            return

        # Post-answer prompt (default no): "Continue chatting?"
        if not typer.confirm(_t("ask_continue_q"), default=False):
            return

        # User opted in → drop into the multi-turn follow-up loop.
        # Plain `input()` misbehaves inside the asyncio event loop on
        # macOS — Enter shows up as a literal `^M` (raw-mode carriage
        # return) and the line never submits. prompt_toolkit's async
        # session correctly hands stdin back and forth with the loop AND
        # supports Cyrillic / non-ASCII typing out of the box.
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML

        console.print(
            "\n[bold cyan]Interactive mode[/] — type a follow-up question (blank or Ctrl-D to exit)."
        )
        prompt_session: PromptSession = PromptSession()

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
    max_cost: float | None,
    yes: bool,
    fallback_pool: list[tuple] | None = None,
    language: str = "en",
    content_language: str = "en",
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
    from analyzetg.core.paths import derive_internal_id
    from analyzetg.util.pricing import chat_cost
    from analyzetg.util.tokens import count_tokens

    tokens = tokenize_question(question)
    candidate_limit = max(limit, ask_cfg.rerank_top_k) if rerank_on else limit

    if semantic:
        from analyzetg.ask.embeddings import default_model as _embed_model_name
        from analyzetg.ask.embeddings import semantic_search

        embed_model = _embed_model_name()
        if chat_ids is None:
            console.print(
                "[red]--semantic needs --chat or --folder.[/] Cosine over every "
                "synced chat would be slow without an ANN index."
            )
            raise typer.Exit(2)
        console.print(
            f"[dim]→ Semantic retrieval[/] ({embed_model}; "
            f"pool={candidate_limit}"
            f"{', rerank→' + str(min(limit, ask_cfg.rerank_keep)) if rerank_on else ''})"
        )
        sem_scored = await semantic_search(
            repo=repo,
            oai=oai,
            question=question,
            chat_ids=chat_ids,
            model=embed_model,
            limit=candidate_limit,
        )
        if not sem_scored:
            console.print(
                "[yellow]No embeddings indexed for this scope.[/] "
                "Run `atg ask --build-index --chat <ref>` (or `--folder`) first."
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
            f"[dim]→ Searching local corpus[/] (tokens: {', '.join(tokens) or '(none)'}; "
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
        from analyzetg.ask.rerank import rerank as _rerank_fn

        keep_n = min(limit, ask_cfg.rerank_keep)
        rerank_model = ask_cfg.rerank_model or settings.openai.filter_model_default
        console.print(
            f"[dim]→ Reranking {len(scored)} candidates with[/] [bold]{rerank_model}[/]"
            f"[dim] → keep top-{keep_n}...[/]"
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
            console.print(f"[dim]{_t('ask_no_matches_reusing')}[/]")
        else:
            console.print(
                "[yellow]No matching messages.[/] Try `atg sync <chat>` first if "
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

    # `content_language` drives LLM-facing strings: system prompt,
    # user-template labels (Question:/Context:/Answer:), chat-group
    # header `=== Chat: ... ===`, and the formatter labels in the
    # context block. `language` is used only for what the user sees
    # rendered by `atg` (cost preview, status messages — already English
    # in the source).
    llm_lang = content_language
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
    # Cost preview against the *full* messages list (system + history + user).
    messages = _build_history_messages(system_prompt, history, user_text)
    prompt_tokens = sum(count_tokens(m["content"], used_model) for m in messages)
    est_cost = chat_cost(used_model, prompt_tokens, 0, 2000, settings=settings)
    if est_cost is not None:
        console.print(
            f"[dim]→ Estimated cost: ~${est_cost:.4f}[/] "
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
            if not typer.confirm(_t("run_anyway_q"), default=False):
                console.print(f"[yellow]{_t('aborted')}[/]")
                raise typer.Exit(0)
    elif max_cost is not None:
        console.print(f"[dim]{_t('max_cost_not_enforced')}[/]")

    console.print(
        f"[dim]{_t('ask_asking_label')}[/] [bold]{used_model}[/] {_tf('ask_over_n_msgs', n=len(msgs))}"
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

    body = (
        f"# {question.strip()}\n\n"
        f"_{len(msgs)} message(s) from {scope_label}, "
        f"model {used_model}, ${float(res.cost_usd or 0):.4f}_\n\n"
        f"{answer}\n"
    )
    if console_out or output is None:
        from rich.markdown import Markdown
        from rich.rule import Rule

        console.print(Rule("answer", style="cyan"))
        console.print(Markdown(body))
        console.print(Rule(style="cyan"))
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")
        console.print(f"[green]{_tf('saved_to_path', path=output)}[/]")
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
) -> None:
    """Forward-direction backfill from each chat's local high-water mark.

    Walks `[max(local msg_id), now]` per chat in parallel (capped at 3
    concurrent backfills to stay friendly to Telegram). Per-chat failures
    log a warning but don't abort the rest of the refresh — `ask` can
    still answer over whatever's already synced.
    """
    import asyncio as _asyncio

    from analyzetg.tg.sync import backfill

    sem = _asyncio.Semaphore(3)
    console.print(f"[dim]{_tf('ask_refreshing', n=len(chat_ids))}[/]")

    async def _one(chat_id: int) -> tuple[int, int | None, str | None]:
        async with sem:
            try:
                local_max = await repo.get_max_msg_id(chat_id, thread_id=thread_id)
                # `from_msg_id=None` with direction=forward would walk from
                # the chat's start; instead pull from local_max forward so
                # we only fetch what's new. If the DB has nothing yet, we
                # still pull recent history (sync.backfill defaults via
                # determine_start when from_msg_id is None).
                fetched = await backfill(
                    client,
                    repo,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    from_msg_id=local_max,
                    direction="forward",
                )
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
        console.print(f"[dim]{_t('ask_refreshed_none')}[/]")
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
