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
from analyzetg.analyzer.openai_client import build_messages, chat_complete, make_client
from analyzetg.ask.retrieval import retrieve_messages, tokenize_question
from analyzetg.config import get_settings
from analyzetg.core.paths import compute_window
from analyzetg.db.repo import open_repo
from analyzetg.tg.client import tg_client
from analyzetg.tg.folders import list_folders, resolve_folder
from analyzetg.tg.resolver import resolve as resolve_ref
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)

# System prompt for the answer call. Russian since most users in this repo
# work with Russian-speaking chats; the model will switch to the question's
# language anyway.
_SYSTEM_PROMPT = (
    "Ты отвечаешь на вопросы пользователя по его архиву Telegram-сообщений. "
    "Опирайся ИСКЛЮЧИТЕЛЬНО на приведённые сообщения. Не выдумывай фактов — "
    "если ответа нет в данных, так и скажи. "
    "Каждое утверждение цитируй markdown-ссылкой [#<msg_id>](<link>), где "
    "link построен подстановкой msg_id в шаблон из строки "
    "'Ссылка на сообщение:' соответствующей группы сообщений. Если шаблона "
    "для группы нет, пиши просто #<msg_id>. Отвечай на языке вопроса, "
    "кратко, по существу."
)


async def cmd_ask(
    *,
    question: str,
    chat: str | None = None,
    thread: int | None = None,
    folder: str | None = None,
    since: str | None = None,
    until: str | None = None,
    last_days: int | None = None,
    limit: int = 200,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    refresh: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
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
    if not question.strip():
        console.print("[red]Empty question.[/]")
        raise typer.Exit(2)
    tokens = tokenize_question(question)
    if not tokens:
        console.print(
            "[yellow]No useful keywords in your question.[/] Add a noun, name, or topic — "
            "stop words and short tokens are filtered."
        )
        raise typer.Exit(2)
    if refresh and not chat and not folder:
        raise typer.BadParameter(
            "--refresh needs --chat or --folder; refusing to backfill every "
            "synced dialog (potentially hundreds of Telegram round-trips)."
        )

    settings = get_settings()
    since_dt, until_dt = compute_window(since, until, last_days)

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        # Resolve scope.
        chat_ids: list[int] | None = None
        chat_titles: dict[int, str] = {}
        if chat:
            console.print(f"[dim]→ Resolving[/] {chat}")
            resolved = await resolve_ref(client, repo, chat)
            chat_ids = [resolved.chat_id]
            chat_titles[resolved.chat_id] = resolved.title or str(resolved.chat_id)
        elif folder:
            folders = await list_folders(client)
            matched = resolve_folder(folder, folders)
            if matched is None:
                titles = ", ".join(f"'{f.title}'" for f in folders) or "(none)"
                console.print(f"[red]No folder matching[/] '{folder}'. Available: {titles}")
                raise typer.Exit(2)
            chat_ids = list(matched.include_chat_ids)
            if not chat_ids:
                console.print(
                    f"[yellow]Folder '{matched.title}' has no explicitly-listed chats[/] "
                    "(rule-based folders aren't expanded)."
                )
                raise typer.Exit(2)
            console.print(f"[dim]→ Folder[/] [bold]{matched.title}[/] — {len(chat_ids)} chat(s)")
        # else: chat_ids stays None → search all synced chats.

        if refresh and chat_ids:
            await _refresh_chats(client, repo, chat_ids, thread_id=thread)

        console.print(
            f"[dim]→ Searching local corpus[/] (tokens: {', '.join(tokens)}; "
            f"limit={limit}{', chat=' + chat if chat else ''}"
            f"{', folder=' + folder if folder else ''})"
        )
        msgs = await retrieve_messages(
            repo=repo,
            question=question,
            chat_ids=chat_ids,
            thread_id=thread,
            since=since_dt,
            until=until_dt,
            limit=limit,
        )
        if not msgs:
            console.print(
                "[yellow]No matching messages.[/] Try `atg sync <chat>` first if "
                "the chat hasn't been backfilled, or broaden your scope (drop --chat / --since)."
            )
            raise typer.Exit(0)

        # Backfill chat titles for any chats we didn't already resolve.
        for m in msgs:
            if m.chat_id in chat_titles:
                continue
            row = await repo.get_chat(m.chat_id)
            chat_titles[m.chat_id] = (row or {}).get("title") or str(m.chat_id)

        # Citation links: each chat needs its own t.me/... template
        # (different username / internal_id / thread). Pre-fetch chat
        # rows so the formatter can emit a `[#msg_id](url)`-friendly
        # template. Without this the LLM has no link pattern and writes
        # bare `#msg_id`, which is what the answer in this run showed.
        from analyzetg.core.paths import derive_internal_id

        chat_links: dict[int, str | None] = {}
        for cid in {m.chat_id for m in msgs}:
            row = await repo.get_chat(cid)
            chat_links[cid] = build_link_template(
                chat_username=(row or {}).get("username"),
                chat_internal_id=derive_internal_id(cid),
                thread_id=thread,
            )

        if chat_ids is not None and len(chat_ids) == 1:
            single_chat_id = chat_ids[0]
            formatted = format_messages(msgs, link_template=chat_links.get(single_chat_id))
            scope_label = chat_titles[single_chat_id]
        else:
            formatted = _format_multi_chat(msgs, chat_titles, chat_links)
            scope_label = f"folder '{folder}'" if folder else "all synced chats"

        oai = make_client()
        used_model = model or settings.openai.chat_model_default
        user_text = (
            f"Вопрос: {question.strip()}\n\n"
            f"Контекст ({len(msgs)} сообщ. из {scope_label}):\n\n"
            f"{formatted}\n\n"
            "Ответ (с цитатами):"
        )

        # Cost preview / --max-cost guard. Unlike analyze (which estimates
        # via _AVG_TOKENS_PER_MSG ≈ 60), ask has the actual prompt body in
        # hand — count tokens precisely so the user sees the real number
        # before $0.30 lands on their bill.
        from analyzetg.util.pricing import chat_cost
        from analyzetg.util.tokens import count_tokens

        prompt_tokens = count_tokens(_SYSTEM_PROMPT, used_model) + count_tokens(user_text, used_model)
        # 2000 = max_tokens passed to chat_complete below.
        est_cost = chat_cost(used_model, prompt_tokens, 0, 2000, settings=settings)
        if est_cost is not None:
            console.print(
                f"[dim]→ Estimated cost: ~${est_cost:.4f}[/] "
                f"({prompt_tokens:,} prompt tokens × {used_model}; output capped at 2000)"
            )
            if max_cost is not None and est_cost > max_cost:
                console.print(f"[bold yellow]⚠ Estimated ${est_cost:.4f} > --max-cost ${max_cost:.4f}[/]")
                if yes:
                    console.print("[red]Aborting (--yes set, no confirmation possible).[/]")
                    raise typer.Exit(2)
                if not typer.confirm("Run anyway?", default=False):
                    console.print("[yellow]Aborted.[/]")
                    raise typer.Exit(0)
        elif max_cost is not None:
            console.print(f"[dim]→ --max-cost not enforced: pricing missing for {used_model}.[/]")

        messages = build_messages(_SYSTEM_PROMPT, "", user_text)
        console.print(f"[dim]→ Asking[/] [bold]{used_model}[/] over {len(msgs)} message(s)...")
        res = await chat_complete(
            oai,
            repo=repo,
            model=used_model,
            messages=messages,
            max_tokens=2000,
            context={"phase": "ask", "scope": scope_label, "tokens": tokens[:10]},
        )
        answer = (res.text or "").strip()
        if not answer:
            console.print("[red]Model returned empty answer.[/]")
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
            console.print(f"[green]Saved[/] {output}")


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
    console.print(f"[dim]→ Refreshing {len(chat_ids)} chat(s) from Telegram...[/]")

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
        console.print(f"[green]Refreshed:[/] {total_new} new message(s) across {len(chat_ids)} chat(s).")
    else:
        console.print("[dim]Refreshed: no new messages.[/]")
    if failed:
        console.print(f"[yellow]⚠ {len(failed)} chat(s) failed to refresh; falling back to local data.[/]")


def _format_multi_chat(
    msgs,
    chat_titles: dict[int, str],
    chat_links: dict[int, str | None],
) -> str:
    """Group messages by chat and render with a chat-title separator.

    Mirrors the topic-grouped format the analyzer uses for flat-forum
    runs — keeps each chat's conversation contiguous so the LLM can
    answer cross-chat questions without losing thread. Each group gets
    its own `Ссылка на сообщение: …` template so citations like
    `[#11537](https://t.me/...)` resolve correctly regardless of which
    chat the msg_id came from.
    """
    from itertools import groupby

    chunks: list[str] = []
    msgs_sorted = sorted(msgs, key=lambda m: (m.chat_id, m.date or datetime.min, m.msg_id))
    for chat_id, group in groupby(msgs_sorted, key=lambda m: m.chat_id):
        title = chat_titles.get(chat_id, str(chat_id))
        chunks.append(f"=== Чат: {title} (id={chat_id}) ===")
        chunks.append(format_messages(list(group), link_template=chat_links.get(chat_id)))
    return "\n\n".join(chunks)
