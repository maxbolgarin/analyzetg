"""Interactive wizard: pick chat → thread → preset → period → run analyze.

The I/O side (`run_interactive`) uses `questionary` for arrow-key menus
with type-to-filter. The pure arg-builder (`build_analyze_args`) turns a
structured answer dict into `cmd_analyze` kwargs — unit-testable without
a Telegram client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import questionary
from prompt_toolkit.keys import Keys
from rich.console import Console

from analyzetg.analyzer.prompts import PRESETS
from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import list_unread_dialogs
from analyzetg.tg.topics import list_forum_topics
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)

# Full-line highlight for the currently-hovered row. Default questionary
# only colours the `»` pointer; this reverses the whole line so selection
# is unmistakable.
LIST_STYLE = questionary.Style(
    [
        # The row the arrow keys are on — full-line reverse for visibility.
        ("highlighted", "reverse bold"),
        # The default (pre-selected) row — just bold, no reverse, so it
        # doesn't compete visually with the hovered one.
        ("selected", "noreverse bold fg:ansigreen"),
        ("pointer", "bold fg:ansicyan"),
        ("qmark", "bold fg:ansicyan"),
        ("question", "bold"),
        ("answer", "fg:ansigreen bold"),
    ]
)


# Sentinel returned by picker helpers when the user chooses "← Back".
# Distinct from None (which means cancel/Ctrl-C).
BACK = object()

# Sentinel returned by _pick_chat when the user picks "Run on all N unread".
ALL_UNREAD = object()


def _bind_escape(question, value):
    """Make ESC exit the questionary prompt with `value`.

    Use `BACK` on steps that have a back action; use `None` on the first step
    (same semantics as Ctrl-C there). `eager=True` so we win over any default
    ESC behaviour (e.g. clearing the search filter)."""

    @question.application.key_bindings.add(Keys.Escape, eager=True)
    def _(event):
        event.app.exit(result=value)

    return question


def _replace_last_line(summary: str) -> None:
    """Erase the line questionary just rendered and write a clean summary.

    Lets us show a columnar list in the picker but keep the post-selection
    echo short (e.g. `? topic: идеи по развитию UNION` instead of dumping
    the full row with counts and separators).
    """
    import sys as _sys

    _sys.stdout.write("\x1b[1A\x1b[2K\r")
    _sys.stdout.flush()
    console.print(summary)


@dataclass(slots=True)
class InteractiveAnswers:
    chat_ref: str
    chat_kind: str
    thread_id: int | None
    forum_all_flat: bool
    forum_all_per_topic: bool
    preset: str
    period: str  # "unread" | "last7" | "last30" | "full" | "custom"
    custom_since: str | None
    custom_until: str | None
    console_out: bool
    mark_read: bool
    output_path: Path | None = None
    run_on_all_unread: bool = False  # User picked "Run on ALL N unread chats"


def build_analyze_args(answers: InteractiveAnswers) -> dict[str, Any]:
    """Turn interactive answers into `cmd_analyze` kwargs. Pure."""
    last_days: int | None = None
    full_history = False
    since: str | None = None
    until: str | None = None
    if answers.period == "last7":
        last_days = 7
    elif answers.period == "last30":
        last_days = 30
    elif answers.period == "full":
        full_history = True
    elif answers.period == "custom":
        since = answers.custom_since
        until = answers.custom_until

    return {
        "ref": answers.chat_ref,
        "thread": answers.thread_id,
        "from_msg": None,
        "full_history": full_history,
        "since": since,
        "until": until,
        "last_days": last_days,
        "preset": answers.preset,
        "prompt_file": None,
        "model": None,
        "filter_model": None,
        "output": answers.output_path,
        "console_out": answers.console_out,
        "mark_read": answers.mark_read,
        "no_cache": False,
        "include_transcripts": True,
        "min_msg_chars": None,
        "all_flat": answers.forum_all_flat,
        "all_per_topic": answers.forum_all_per_topic,
    }


async def run_interactive_analyze(
    *,
    console_out: bool = False,
    output: Path | None = None,
    mark_read: bool = False,
) -> None:
    """Default UX for `analyzetg analyze` (no ref). Walk wizard, then run."""
    answers = await _collect_answers(
        mode="analyze",
        console_out=console_out,
        output=output,
        mark_read=mark_read,
    )
    if answers is None:
        return
    # Wizard's Telegram session is already closed. Dispatching now opens a
    # new one inside whichever command we hand off to.
    if answers.run_on_all_unread:
        from analyzetg.analyzer.commands import run_all_unread_analyze

        await run_all_unread_analyze(
            preset=answers.preset,
            output=output,
            console_out=console_out,
            mark_read=answers.mark_read,
        )
        return

    from analyzetg.analyzer.commands import cmd_analyze

    await cmd_analyze(**build_analyze_args(answers))


async def run_interactive_dump(
    *,
    fmt: str = "md",
    output: Path | None = None,
    with_transcribe: bool = False,
    include_transcripts: bool = True,
    console_out: bool = False,
    mark_read: bool = False,
) -> None:
    """Default UX for `analyzetg dump` (no ref). Wizard without preset step."""
    answers = await _collect_answers(
        mode="dump",
        console_out=console_out,
        output=output,
        mark_read=mark_read,
    )
    if answers is None:
        return

    if answers.run_on_all_unread:
        from analyzetg.export.commands import run_all_unread_dump

        await run_all_unread_dump(
            fmt=fmt,
            output=output,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=console_out,
            mark_read=answers.mark_read,
        )
        return

    from analyzetg.export.commands import cmd_dump

    # Translate answers → cmd_dump kwargs (period → since/until/last_days).
    last_days: int | None = None
    full_history = False
    since: str | None = None
    until: str | None = None
    if answers.period == "last7":
        last_days = 7
    elif answers.period == "last30":
        last_days = 30
    elif answers.period == "full":
        full_history = True
    elif answers.period == "custom":
        since = answers.custom_since
        until = answers.custom_until

    await cmd_dump(
        ref=answers.chat_ref,
        output=output,
        fmt=fmt,
        since=since,
        until=until,
        last_days=last_days,
        full_history=full_history,
        thread=answers.thread_id,
        from_msg=None,
        join=False,
        with_transcribe=with_transcribe,
        include_transcripts=include_transcripts,
        console_out=console_out,
        mark_read=answers.mark_read,
        all_flat=answers.forum_all_flat,
        all_per_topic=answers.forum_all_per_topic,
    )


async def run_interactive_describe() -> None:
    """Default UX for `analyzetg describe` (no ref, no filters): pick → show."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        console.print("[bold cyan]analyzetg[/] — pick a chat to describe")
        console.print(
            "[dim]Tips: ↑/↓ to navigate, type to filter, Enter to select, ESC or Ctrl-C to cancel.[/]\n"
        )
        chat = await _pick_chat(client, offer_all_unread=False)
        if chat is None or chat is ALL_UNREAD:
            console.print("[dim]Cancelled.[/]")
            return
        chat_ref = str(chat["chat_id"])

    # Now open a fresh session via the existing cmd_describe flow.
    from analyzetg.tg.commands import cmd_describe

    await cmd_describe(chat_ref)


async def _collect_answers(
    *,
    mode: str,  # "analyze" | "dump"
    console_out: bool,
    output: Path | None,
    mark_read: bool,
) -> InteractiveAnswers | None:
    """State-machine wizard: each step can go back one without losing context.

    `mode` controls which steps appear: "analyze" walks through preset;
    "dump" skips the preset step.
    """
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        console.print("[bold cyan]analyzetg[/] — interactive mode")
        # Show the immutable settings so the user knows what will happen.
        out_label = (
            "console (rendered markdown)"
            if console_out
            else (f"{output}" if output else "reports/ (auto-named file)")
        )
        console.print(f"  [dim]output:[/]    [bold]{out_label}[/]")
        console.print(
            f"  [dim]mark read:[/] [bold]{'yes' if mark_read else 'no'}[/]"
            + ("" if mark_read else " [dim](use --mark-read to enable)[/]")
        )
        if not console_out and not output:
            console.print(
                "  [dim]↳ pass[/] [cyan]--console[/] / [cyan]-c[/] [dim]or[/] "
                "[cyan]-o <path>[/] [dim]to change output.[/]"
            )
        console.print(
            "[dim]Tips: ↑/↓ to navigate, type to filter, Enter to select, "
            "ESC to go back (Ctrl-C to cancel).[/]\n"
        )

        chat: dict | None = None
        thread_id: int | None = None
        forum_all_flat = False
        forum_all_per_topic = False
        preset: str | None = None
        period: str | None = None
        custom_since: str | None = None
        custom_until: str | None = None

        run_on_all = False
        step = "chat"
        while True:
            if step == "chat":
                result = await _pick_chat(client, offer_all_unread=True)
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                if result is ALL_UNREAD:
                    run_on_all = True
                    # Still let the user pick a preset for analyze; skip
                    # everything else (thread/period/custom-range) — batch
                    # is always "each chat's own unread".
                    step = "preset" if mode == "analyze" else "confirm"
                    continue
                chat = result
                step = "thread" if chat["kind"] == "forum" else ("preset" if mode == "analyze" else "period")

            elif step == "thread":
                result = await _pick_thread(client, chat["chat_id"])
                if result is BACK:
                    step = "chat"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                thread_id, forum_all_flat, forum_all_per_topic = result
                step = "preset" if mode == "analyze" else "period"

            elif step == "preset":
                # Only runs for analyze mode.
                result = await _pick_preset()
                if result is BACK:
                    step = "chat" if run_on_all else ("thread" if chat["kind"] == "forum" else "chat")
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                preset = result
                step = "mark_read" if run_on_all else "period"

            elif step == "period":
                result = await _pick_period(force_explicit=forum_all_flat)
                if result is BACK:
                    if mode == "analyze":
                        step = "preset"
                    else:
                        step = "thread" if chat and chat["kind"] == "forum" else "chat"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                period, custom_since, custom_until = result
                step = "mark_read"

            elif step == "mark_read":
                result = await _pick_mark_read(default=mark_read)
                if result is BACK:
                    step = ("preset" if mode == "analyze" else "chat") if run_on_all else "period"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                mark_read = bool(result)
                step = "confirm"

            elif step == "confirm":
                summary_bits = []
                if run_on_all:
                    summary_bits.append("ALL unread chats (batch)")
                else:
                    summary_bits.append(chat.get("title") or str(chat["chat_id"]))
                if thread_id:
                    summary_bits.append(f"topic {thread_id}")
                if forum_all_flat:
                    summary_bits.append("all-flat")
                if forum_all_per_topic:
                    summary_bits.append("per-topic")
                if mode == "analyze":
                    summary_bits.append(f"preset={preset}")
                if not run_on_all:
                    summary_bits.append(f"period={period}")
                    if period == "custom":
                        summary_bits.append(f"({custom_since or ''}..{custom_until or ''})")
                summary_bits.append("console" if console_out else "save to reports/")
                if mark_read:
                    summary_bits.append("mark-read")
                console.print("[bold]Plan:[/] " + " / ".join(summary_bits))

                choice = await _bind_escape(
                    questionary.select(
                        "Run it?",
                        choices=[
                            questionary.Choice("Run", value="run"),
                            questionary.Choice("← Back", value=BACK),
                            questionary.Choice("Cancel", value="cancel"),
                        ],
                        style=LIST_STYLE,
                    ),
                    BACK,
                ).ask_async()
                if choice is None or choice == "cancel":
                    console.print("[dim]Cancelled.[/]")
                    return None
                if choice is BACK:
                    step = "mark_read"
                    continue
                break

        return InteractiveAnswers(
            chat_ref="" if run_on_all else str(chat["chat_id"]),
            chat_kind="" if run_on_all else chat["kind"],
            thread_id=thread_id,
            forum_all_flat=forum_all_flat,
            forum_all_per_topic=forum_all_per_topic,
            preset=preset if preset is not None else "summary",
            period=period if period is not None else "unread",
            custom_since=custom_since,
            custom_until=custom_until,
            console_out=console_out,
            mark_read=mark_read,
            output_path=output,
            run_on_all_unread=run_on_all,
        )


def _fmt_count(n: int) -> str:
    """Right-align a count in a 6-char field; '     —' if zero."""
    return f"{n:>6}" if n else "     —"


def _fmt_date(dt: datetime | None) -> str:
    """Compact date for picker rows. 12-char fixed width for column alignment."""
    if dt is None:
        return "—           "
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    delta_s = (now - dt).total_seconds()
    if -60 < delta_s < 24 * 3600:
        return dt.strftime("       %H:%M")  # within last 24h → HH:MM only
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")  # this year → "Apr 23 09:14"
    return dt.strftime("%Y-%m-%d  ")  # older → full date


async def _fetch_first_unread_dates(client, dialogs: list) -> dict[int, datetime | None]:
    """For each dialog, the date of the oldest unread message.

    Uses `get_messages(chat, limit=1, min_id=marker, reverse=True)` to grab
    the oldest real message above the per-dialog read marker (skipping
    deleted msg-ids). Parallel with a cap of 5 in-flight. Errors are logged
    (run `analyzetg -v ...` to see) and fall back to None.
    """
    import asyncio as _asyncio

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    sem = _asyncio.Semaphore(5)
    resolved = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]Fetching first-unread dates[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("fetch", total=len(dialogs))

        async def one(d) -> tuple[int, datetime | None]:
            nonlocal resolved
            if not d.read_inbox_max_id:
                log.debug("first_unread.no_marker", chat_id=d.chat_id, kind=d.kind)
                progress.advance(task)
                return d.chat_id, None
            try:
                async with sem:
                    msgs = await client.get_messages(
                        d.chat_id,
                        limit=1,
                        min_id=d.read_inbox_max_id,
                        reverse=True,
                    )
                if msgs:
                    resolved += 1
                    return d.chat_id, getattr(msgs[0], "date", None)
                log.debug(
                    "first_unread.empty_result",
                    chat_id=d.chat_id,
                    marker=d.read_inbox_max_id,
                )
                return d.chat_id, None
            except Exception as e:
                log.warning(
                    "first_unread.error",
                    chat_id=d.chat_id,
                    marker=d.read_inbox_max_id,
                    err=str(e)[:200],
                )
                return d.chat_id, None
            finally:
                progress.advance(task)

        results = await _asyncio.gather(*[one(d) for d in dialogs])
    if resolved < len(dialogs):
        console.print(
            f"[dim]→ First-unread dates resolved for {resolved}/{len(dialogs)} "
            "chats (remaining shown as —; run with -v to see errors).[/]"
        )
    return dict(results)


async def _pick_chat(client, *, offer_all_unread: bool = False) -> dict | None | object:
    """Show dialogs with unread (sorted by count desc), offer all-dialogs fallback.

    Returns one of:
      - dict (picked chat) — a resolved entry
      - ALL_UNREAD — user picked "Run on all N unread chats" (if offer_all_unread)
      - None — cancelled
    """
    unread = await list_unread_dialogs(client)

    if not unread:
        console.print("[yellow]No chats with unread messages. Showing all dialogs.[/]")
        return await _pick_from_all(client)

    first_dates = await _fetch_first_unread_dates(client, unread)

    # Column header as a non-selectable separator at the top.
    header = f"{'unread':>6}  · {'kind':<11} · {'first unread':<12} · {'last msg':<12} · title"
    choices: list[Any] = []
    if offer_all_unread:
        total = sum(d.unread_count for d in unread)
        choices.append(
            questionary.Choice(
                title=f"🚀  Run on ALL {len(unread)} unread chats ({total} total messages)",
                value=("all_unread", None),
            )
        )
        choices.append(questionary.Separator())
    choices.append(questionary.Separator(header))
    for d in unread:
        choices.append(
            questionary.Choice(
                title=(
                    f"{_fmt_count(d.unread_count)}  · "
                    f"{d.kind:<11} · "
                    f"{_fmt_date(first_dates.get(d.chat_id)):<12} · "
                    f"{_fmt_date(d.last_msg_date):<12} · "
                    f"{d.title or d.chat_id}"
                ),
                value=("pick", d),
            )
        )
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title="🔍  Search all dialogs (not just unread)", value=("all", None)))
    # No "Back" on the first step — there's nowhere to go back to.
    # Ctrl-C cancels the whole wizard.

    result = await _bind_escape(
        questionary.select(
            f"Pick a chat — {len(unread)} with unread, sorted by count (type to filter, ↑/↓ to move)",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        None,
    ).ask_async()

    if result is None:
        return None
    action, payload = result
    if action == "all_unread":
        _replace_last_line("[bold cyan]?[/] chat: [bold]ALL unread chats[/]")
        return ALL_UNREAD
    if action == "all":
        _replace_last_line("[bold cyan]?[/] chat: [dim](searching all dialogs)[/]")
        return await _pick_from_all(client)
    d = payload
    _replace_last_line(
        f"[bold cyan]?[/] chat: [bold]{d.title or d.chat_id}[/] [dim]({d.kind}, {d.unread_count} unread)[/]"
    )
    return {
        "chat_id": d.chat_id,
        "kind": d.kind,
        "title": d.title,
        "username": d.username,
    }


async def _pick_from_all(client) -> dict | None:
    """Scan every dialog and present a searchable list."""
    from analyzetg.tg.client import _chat_kind, entity_id, entity_title, entity_username

    rows: list[dict] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = d.entity
        rows.append(
            {
                "chat_id": entity_id(entity),
                "kind": _chat_kind(entity),
                "title": entity_title(entity),
                "username": entity_username(entity),
                "unread": int(getattr(d, "unread_count", 0) or 0),
            }
        )
    if not rows:
        console.print("[yellow]No dialogs at all.[/]")
        return None

    # Sort: unread desc, then alpha title.
    rows.sort(key=lambda r: (-r["unread"], (r["title"] or "").lower()))

    choices = [
        questionary.Choice(
            title=f"{_fmt_count(r['unread'])}  · {r['kind']:<11} · {r['title'] or r['chat_id']}",
            value=r,
        )
        for r in rows
    ]

    picked = await _bind_escape(
        questionary.select(
            f"Pick a chat from {len(rows)} dialogs (type to filter, ↑/↓ to move)",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        None,
    ).ask_async()
    if picked is not None:
        _replace_last_line(
            f"[bold cyan]?[/] chat: [bold]{picked['title'] or picked['chat_id']}[/] "
            f"[dim]({picked['kind']}" + (f", {picked['unread']} unread" if picked["unread"] else "") + ")[/]"
        )
    return picked


async def _pick_thread(client, chat_id: int):
    """Return (thread_id, all_flat, all_per_topic), BACK, or None (cancelled)."""
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print("[yellow]No topics in this forum.[/]")
        return None

    # Sort: unread desc, then pinned first, then alpha.
    topics_sorted = sorted(topics, key=lambda t: (-t.unread_count, not t.pinned, t.title.lower()))

    choices: list[Any] = [
        questionary.Choice(
            title=(f"{_fmt_count(t.unread_count)}  · {'📌 ' if t.pinned else '   '}{t.title}"),
            value=("thread", t.topic_id),
        )
        for t in topics_sorted
    ]
    choices.insert(0, questionary.Separator("── Forum modes ──"))
    choices.insert(
        1,
        questionary.Choice(
            title="📚 Per-topic: one report per topic with unread (default)",
            value=("per_topic", None),
        ),
    )
    choices.insert(
        2,
        questionary.Choice(
            title="🔀 All-flat: whole forum as one analysis (needs explicit period)",
            value=("flat", None),
        ),
    )
    choices.insert(3, questionary.Separator("── Pick a single topic ──"))
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title="← Back", value=("back", None)))

    result = await _bind_escape(
        questionary.select(
            f"{len(topics)} topic(s) in this forum",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        ("back", None),
    ).ask_async()

    if result is None:
        return None
    action, payload = result
    if action == "back":
        _replace_last_line("[dim]← Back[/]")
        return BACK
    if action == "per_topic":
        _replace_last_line("[bold cyan]?[/] mode: [bold]per-topic[/] [dim](one report per topic)[/]")
        return None, False, True
    if action == "flat":
        _replace_last_line("[bold cyan]?[/] mode: [bold]all-flat[/] [dim](whole forum as one analysis)[/]")
        return None, True, False
    picked_topic = next((t for t in topics_sorted if t.topic_id == payload), None)
    label = picked_topic.title if picked_topic else str(payload)
    _replace_last_line(f"[bold cyan]?[/] topic: [bold]{label}[/]")
    return payload, False, False


async def _pick_preset():
    """Returns preset name (str), BACK, or None."""
    preferred = [
        "summary",
        "digest",
        "highlights",
        "action_items",
        "decisions",
        "questions",
        "quotes",
        "links",
    ]
    names = [p for p in preferred if p in PRESETS]
    names += [n for n in sorted(PRESETS.keys()) if n not in preferred]

    descriptions = {
        "summary": "Топ-3 темы + тезисы + ключевые сообщения (дефолт)",
        "digest": "Короткий дайджест 5–10 тем",
        "action_items": "Задачи из чата — таблица кто/что/срок/статус",
        "decisions": "Принятые решения — таблица решение/кто/когда",
        "highlights": "Топ 5–15 самых ценных сообщений с ссылками",
        "questions": "Открытые вопросы, на которые стоит вернуться",
        "quotes": "Памятные цитаты дословно с автором и ссылкой",
        "links": "Внешние URL из чата, сгруппированные по темам",
    }
    choices: list[Any] = [
        questionary.Choice(
            title=f"{n:<13} — {descriptions.get(n, PRESETS[n].prompt_version)}",
            value=n,
        )
        for n in names
    ]
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title="← Back", value=BACK))

    picked = await _bind_escape(
        questionary.select(
            "Pick a preset",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()
    if picked is None:
        return None
    if picked is BACK:
        _replace_last_line("[dim]← Back[/]")
        return BACK
    _replace_last_line(f"[bold cyan]?[/] preset: [bold]{picked}[/]")
    return picked


async def _pick_mark_read(*, default: bool):
    """Yes/No/Back. Returns True, False, BACK, or None (cancel)."""
    choices = [
        questionary.Choice("No — keep messages unread in Telegram", value=False),
        questionary.Choice("Yes — advance Telegram's read marker after analysis", value=True),
        questionary.Separator(),
        questionary.Choice("← Back", value=BACK),
    ]
    picked = await _bind_escape(
        questionary.select(
            "Mark the processed messages as read?",
            choices=choices,
            default=bool(default),
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()
    if picked is None:
        return None
    if picked is BACK:
        _replace_last_line("[dim]← Back[/]")
        return BACK
    _replace_last_line(f"[bold cyan]?[/] mark-read: [bold]{'yes' if picked else 'no'}[/]")
    return picked


async def _pick_period(*, force_explicit: bool):
    """Returns (period_key, since, until), BACK, or None."""
    options: list[Any] = []
    if not force_explicit:
        options.append(
            questionary.Choice(title="Unread (default) — since Telegram read marker", value="unread")
        )
    options.extend(
        [
            questionary.Choice(title="Last 7 days", value="last7"),
            questionary.Choice(title="Last 30 days", value="last30"),
            questionary.Choice(title="Full history", value="full"),
            questionary.Choice(title="Custom date range…", value="custom"),
            questionary.Separator(),
            questionary.Choice(title="← Back", value=BACK),
        ]
    )
    key = await _bind_escape(
        questionary.select(
            "Pick a period",
            choices=options,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()
    if key is None:
        return None
    if key is BACK:
        _replace_last_line("[dim]← Back[/]")
        return BACK
    _period_labels = {
        "unread": "unread (since Telegram read marker)",
        "last7": "last 7 days",
        "last30": "last 30 days",
        "full": "full history",
        "custom": "custom range",
    }
    _replace_last_line(f"[bold cyan]?[/] period: [bold]{_period_labels.get(key, key)}[/]")
    if key == "custom":
        since = await questionary.text("From (YYYY-MM-DD, blank for open)", default="").ask_async()
        until = await questionary.text("Until (YYYY-MM-DD, blank for open)", default="").ask_async()
        if since is None or until is None:
            return None
        for val in (since, until):
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    console.print(f"[red]Bad date:[/] {val} (expected YYYY-MM-DD)")
                    return await _pick_period(force_explicit=force_explicit)
        return key, since or None, until or None
    return key, None, None


__all__ = [
    "ALL_UNREAD",
    "BACK",
    "InteractiveAnswers",
    "Path",
    "build_analyze_args",
    "run_interactive_analyze",
    "run_interactive_describe",
    "run_interactive_dump",
]
