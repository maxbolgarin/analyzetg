"""Interactive wizard: pick chat → thread → preset → period → run analyze.

The I/O side (`run_interactive`) uses `questionary` for arrow-key menus
with type-to-filter. The pure arg-builder (`build_analyze_args`) turns a
structured answer dict into `cmd_analyze` kwargs — unit-testable without
a Telegram client.
"""

from __future__ import annotations

import asyncio as _asyncio
import string as _string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import questionary
from prompt_toolkit.keys import Keys
from rich.console import Console

from unread.analyzer.prompts import Preset, get_presets
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as i18n_t
from unread.i18n import tf as i18n_tf
from unread.tg.client import tg_client
from unread.tg.dialogs import list_unread_dialogs
from unread.tg.topics import list_forum_topics
from unread.util.logging import get_logger
from unread.util.prompt import _SELECTED_STYLE as _SHARED_PROMPT_STYLE


def _expand_printable_for_search() -> None:
    """Let questionary's `use_search_filter=True` accept non-ASCII input.

    questionary registers a key binding per character in `string.printable`
    (ASCII only — 100 chars) and silently drops everything else via a
    catch-all `Keys.Any` no-op. We expand `string.printable` here so the
    same registration loop covers Cyrillic, Latin Extended (accented
    letters), Greek, Hebrew, and Arabic — i.e. type-to-filter just works
    for chats whose titles aren't Latin.

    CJK isn't included on purpose: tens of thousands of glyphs would
    register tens of thousands of prompt_toolkit bindings per picker;
    terminal IMEs for those scripts also interact poorly with single-key
    filter bindings.
    """
    if any(ord(c) > 127 for c in _string.printable):
        return
    extra: list[str] = []
    for start, end in (
        (0x00A0, 0x024F),  # Latin-1 Supplement + Latin Extended-A/B (umlauts, accents)
        (0x0370, 0x03FF),  # Greek
        (0x0400, 0x052F),  # Cyrillic + Cyrillic Supplement
        (0x0590, 0x06FF),  # Hebrew + Arabic
    ):
        for cp in range(start, end + 1):
            ch = chr(cp)
            if ch.isprintable():
                extra.append(ch)
    _string.printable = _string.printable + "".join(extra)


_expand_printable_for_search()

console = Console()
log = get_logger(__name__)

# All wizards / pickers across the CLI share `unread.util.prompt`'s
# style so the look-and-feel is identical between the init wizard
# (which routes through `prompt.select`) and the analyze wizard
# (which still calls `questionary.select` directly for the chat /
# topic / preset / period table layouts). One source of truth: edit
# `_SELECTED_STYLE` in `unread/util/prompt.py` to restyle everywhere.
LIST_STYLE = questionary.Style(list(_SHARED_PROMPT_STYLE))


# Sentinel returned by picker helpers when the user chooses "← Back".
# Distinct from None (which means cancel/Ctrl-C).
BACK = object()

# Sentinel returned by _pick_chat when the user picks "Run on all N unread".
ALL_UNREAD = object()

# Sentinel returned by _pick_chat when the user picks "Search ALL
# synced chats (local DB)". Triggers the no-scope local query path —
# zero TG round-trips, retrieval reads every synced chat.
ALL_LOCAL = object()


@dataclass(slots=True, frozen=True)
class _PickedFolder:
    """Returned by `_pick_chat` when the user picks "Run on a folder…"
    and selects a folder. Carries the folder title so the wizard can
    forward it to `run_all_unread_*(folder=...)`.
    """

    name: str


# Rough token estimate per formatted message line (sender + timestamp + body).
# Used only for up-front cost previews; the real pipeline counts exactly via
# tiktoken. Cyrillic runs ~1.5x the English rate — this is a middle ground.
_AVG_TOKENS_PER_MSG = 60


# Window for the double-Esc "exit wizard" shortcut. 500ms matches the
# OS-typical double-click window — short enough that deliberate back-
# stepping (Esc · pause · Esc) won't trip it, long enough that a quick
# Esc-Esc tap registers as "get me out" rather than two single backs.
_DOUBLE_ESC_WINDOW_S: float = 0.5

# Module-level timestamp of the most recent Esc key event (across all
# prompts). prompt_toolkit's key handler exits the current prompt on
# the first Esc, so we cannot detect Esc-Esc within a single picker —
# instead we straddle prompts: if the user presses Esc on picker N and
# then Esc on picker N+1 within the window, the second Esc cancels the
# whole wizard. Single-threaded asyncio, so no lock needed.
_LAST_ESC_AT: float = 0.0


def _is_double_esc(now: float, last_esc_at: float, window: float) -> bool:
    """Decide whether `now` qualifies as the second tap of a double-Esc.

    Extracted so the timing logic in `_bind_escape` is unit-testable
    without spinning up prompt_toolkit. The strict `0 <` lower bound
    rules out the very first Esc (where `last_esc_at` is the initial
    `0.0` sentinel), which would otherwise satisfy `now - 0 <= window`
    on a fresh interpreter and mis-classify a single Esc as a double.
    """
    return 0 < (now - last_esc_at) <= window


def _bind_escape(question, value):
    """Make ESC exit the questionary prompt with `value`.

    Use `BACK` on steps that have a back action; use `None` on the first
    step (same semantics as Ctrl-C there). `eager=True` so we win over
    any default ESC behaviour (e.g. clearing the search filter).

    Double-tap exit: pressing Esc twice within `_DOUBLE_ESC_WINDOW_S`
    exits the prompt with `None` (full cancel) regardless of the
    caller's `value`. Wizard callers already treat a `None` result as
    "user cancelled" and unwind the loop.
    """
    import time as _time

    @question.application.key_bindings.add(Keys.Escape, eager=True)
    def _(event):
        global _LAST_ESC_AT
        now = _time.monotonic()
        if _is_double_esc(now, _LAST_ESC_AT, _DOUBLE_ESC_WINDOW_S):
            # Second Esc within the window → cancel the wizard entirely.
            # Reset the timestamp so a third stray press doesn't get
            # mis-classified against this same window.
            _LAST_ESC_AT = 0.0
            event.app.exit(result=None)
            return
        _LAST_ESC_AT = now
        event.app.exit(result=value)

    return question


def _bind_arrow_checkbox(question):
    """Extend questionary.checkbox with directional arrow-key selection.

    Default questionary bindings: Space toggles. That's fine but requires
    reaching for the spacebar per row. Users expect `→` to check the
    current row and `←` to uncheck it — matching how many TUI checkbox
    lists behave. We reach into the prompt's internal `InquirerControl`
    via `layout.walk()` (the same control questionary's Space handler
    uses) and mutate `selected_options` directly. prompt_toolkit
    re-renders on every keystroke, so no explicit invalidate needed.

    Silently no-ops if we can't find the control (defensive against
    future questionary internal changes) — the user still has Space
    available as a fallback.
    """
    try:
        from questionary.prompts.common import InquirerControl  # type: ignore[import-not-found]
    except ImportError:
        return question

    ic = None
    try:
        for container in question.application.layout.walk():
            content = getattr(container, "content", None)
            if isinstance(content, InquirerControl):
                ic = content
                break
    except Exception:
        return question
    if ic is None:
        return question

    bindings = question.application.key_bindings

    @bindings.add(Keys.Right, eager=True)
    def _toggle_right(event):
        # → toggles: select if unchecked, deselect if checked. Mirrors how most
        # users instinctively retry → after realizing they want to uncheck;
        # forcing them to reach for ← or Space for that one row was friction.
        choice = ic.get_pointed_at()
        if choice is None:
            return
        if choice.value in ic.selected_options:
            ic.selected_options.remove(choice.value)
        else:
            ic.selected_options.append(choice.value)

    @bindings.add(Keys.Left, eager=True)
    def _deselect(event):
        choice = ic.get_pointed_at()
        if choice is None:
            return
        if choice.value in ic.selected_options:
            ic.selected_options.remove(choice.value)

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
    # `None` only in ask mode (preset is meaningless there); analyze mode
    # always sets this and dump mode falls back to the existing default.
    preset: str | None
    # Period keys: "unread" | "last24h" | "last96h" | "last7" | "last30" |
    # "last90" | "year_start" | "full" | "custom" | "from_msg"
    period: str
    custom_since: str | None
    custom_until: str | None
    console_out: bool
    mark_read: bool
    output_path: Path | None = None
    # When True (only valid alongside `console_out=True` and
    # `output_path=None`): render the report to the terminal AND save a
    # copy to the default reports/ directory. New wizard default for dump
    # so users see the rendered output without losing the saved file.
    also_save_default: bool = False
    run_on_all_unread: bool = False  # User picked "Run on ALL N unread chats"
    run_on_all_local: bool = False  # ask mode: "🌐 ALL synced chats" picked
    # Folder name when the user picked "Run on a folder…". When set,
    # `run_on_all_unread` is also True (the underlying batch flow is
    # the same; the folder name narrows the scope to chats in that
    # Telegram folder).
    run_on_folder: str | None = None
    # None = "use defaults" (config.toml + preset); [] = "disable everything";
    # non-empty list = "enable exactly these kinds" (unioned with preset.enrich_kinds
    # by cmd_analyze via --enrich=<csv>).
    enrich_kinds: list[str] | None = None
    # Set only when period == "from_msg": a Telegram message link OR bare
    # msg_id string. Passed through to cmd_analyze's --from-msg unchanged
    # (cmd_analyze does the link parsing).
    custom_from_msg: str | None = None
    # Channel + comments toggle. Asked only when the picked chat is a
    # channel that has a linked discussion group; default False otherwise.
    with_comments: bool = False


def _period_to_db_filters(
    *,
    period: str | None,
    custom_since: str | None,
    custom_until: str | None,
    custom_from_msg: str | None,
    chat: dict[str, Any] | None,
) -> dict[str, Any]:
    """Translate the wizard's period choice into `Repo.media_breakdown` kwargs.

    "unread" maps to `min_msg_id = chat.read_inbox_max_id` (matching the
    invariant that unread is `msg_id > read_inbox_max_id`). last7/last30
    map to a since-cutoff. "full" returns no filter. "from_msg" parses the
    user's link/id. Empty kwargs are filtered out so `media_breakdown` sees
    only the dimensions the user actually chose.
    """
    out: dict[str, Any] = {}
    if period == "unread" and chat:
        marker = int(chat.get("read_inbox_max_id") or 0)
        if marker > 0:
            out["min_msg_id"] = marker
    elif period == "last24h":
        out["since"] = datetime.now(UTC) - timedelta(hours=24)
    elif period == "last96h":
        out["since"] = datetime.now(UTC) - timedelta(hours=96)
    elif period == "last7":
        out["since"] = datetime.now(UTC) - timedelta(days=7)
    elif period == "last30":
        out["since"] = datetime.now(UTC) - timedelta(days=30)
    elif period == "last90":
        out["since"] = datetime.now(UTC) - timedelta(days=90)
    elif period == "year_start":
        now_utc = datetime.now(UTC)
        out["since"] = datetime(now_utc.year, 1, 1, tzinfo=UTC)
    elif period == "custom":
        if custom_since:
            out["since"] = datetime.strptime(custom_since, "%Y-%m-%d").replace(tzinfo=UTC)
        if custom_until:
            out["until"] = datetime.strptime(custom_until, "%Y-%m-%d").replace(tzinfo=UTC)
    elif period == "from_msg" and custom_from_msg:
        from unread.tg.links import parse as _parse_link

        try:
            if custom_from_msg.lstrip("-").isdigit():
                out["min_msg_id"] = int(custom_from_msg) - 1  # exclusive lower bound
            else:
                msg_id = _parse_link(custom_from_msg).msg_id
                if msg_id is not None:
                    out["min_msg_id"] = msg_id - 1
        except Exception:
            pass  # fall through to no filter; counts will over-estimate
    # period == "full" → no filter.
    return out


def _build_period_kwargs(answers: InteractiveAnswers, *, include_from_msg: bool) -> dict[str, Any]:
    """Map the wizard's `period` choice to CLI kwargs.

    `include_from_msg=True` for analyze (which supports `--from-msg`),
    `False` for dump (which does not). Extracted so analyze and dump
    don't drift on period semantics.
    """
    last_days: int | None = None
    last_hours: int | None = None
    full_history = False
    since: str | None = None
    until: str | None = None
    from_msg: str | None = None
    if answers.period == "last24h":
        last_hours = 24
    elif answers.period == "last96h":
        last_hours = 96
    elif answers.period == "last7":
        last_days = 7
    elif answers.period == "last30":
        last_days = 30
    elif answers.period == "last90":
        last_days = 90
    elif answers.period == "year_start":
        # `since=YYYY-01-01` flows through CLI's --since path; UTC-midnight
        # parse happens in core.paths.parse_ymd.
        since = f"{datetime.now(UTC).year}-01-01"
    elif answers.period == "full":
        full_history = True
    elif answers.period == "custom":
        since = answers.custom_since
        until = answers.custom_until
    elif answers.period == "from_msg" and include_from_msg:
        from_msg = answers.custom_from_msg
    out: dict[str, Any] = {
        "last_days": last_days,
        "last_hours": last_hours,
        "full_history": full_history,
        "since": since,
        "until": until,
    }
    if include_from_msg:
        out["from_msg"] = from_msg
    return out


def _build_enrich_kwargs(answers: InteractiveAnswers) -> dict[str, Any]:
    """Translate wizard's enrich_kinds tri-state into CLI flags.

    None (wizard skipped / defaults) → pass nothing, cmd_analyze/dump
    will use config. [] → no_enrich=True. Populated list → --enrich=<csv>.
    """
    enrich_csv: str | None = None
    no_enrich = False
    if answers.enrich_kinds is not None:
        if not answers.enrich_kinds:
            no_enrich = True
        else:
            enrich_csv = ",".join(answers.enrich_kinds)
    return {"enrich": enrich_csv, "enrich_all": False, "no_enrich": no_enrich}


def build_analyze_args(answers: InteractiveAnswers) -> dict[str, Any]:
    """Turn interactive answers into `cmd_analyze` kwargs. Pure."""
    return {
        "ref": answers.chat_ref,
        "thread": answers.thread_id,
        **_build_period_kwargs(answers, include_from_msg=True),
        "preset": answers.preset,
        "prompt_file": None,
        "model": None,
        "filter_model": None,
        "output": answers.output_path,
        "console_out": answers.console_out,
        "save_default": False,
        "mark_read": answers.mark_read,
        "no_cache": False,
        "include_transcripts": True,
        "min_msg_chars": None,
        **_build_enrich_kwargs(answers),
        # The wizard already asked "Run it?" at the confirm step via
        # questionary. Passing yes=True here prevents cmd_analyze from
        # double-confirming via typer.confirm — that second prompt is
        # what causes "Enter doesn't work" after a prompt-toolkit session
        # in some terminals (Cursor, VS Code integrated, etc.).
        "yes": True,
        "all_flat": answers.forum_all_flat,
        "all_per_topic": answers.forum_all_per_topic,
    }


def build_dump_args(
    answers: InteractiveAnswers, *, fmt: str, with_transcribe: bool, include_transcripts: bool
) -> dict[str, Any]:
    """Turn interactive answers into `cmd_dump` kwargs. Pure."""
    return {
        "ref": answers.chat_ref,
        "output": answers.output_path,
        "fmt": fmt,
        **_build_period_kwargs(answers, include_from_msg=False),
        "thread": answers.thread_id,
        "from_msg": None,
        "join": False,
        "with_transcribe": with_transcribe,
        "include_transcripts": include_transcripts,
        "console_out": answers.console_out,
        "also_save_default": answers.also_save_default,
        "save_default": False,
        "mark_read": answers.mark_read,
        "all_flat": answers.forum_all_flat,
        "all_per_topic": answers.forum_all_per_topic,
        "with_comments": bool(answers.with_comments),
        **_build_enrich_kwargs(answers),
    }


async def run_interactive_analyze(
    *,
    console_out: bool = False,
    output: Path | None = None,
    save_default: bool = False,
    mark_read: bool | None = None,
    post_saved: bool = False,
    max_cost: float | None = None,
    self_check: bool = False,
    cite_context: bool = False,
    no_cache: bool = False,
    dry_run: bool = False,
    by: str | None = None,
    post_to: str | None = None,
    with_comments: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
) -> None:
    """Default UX for `unread analyze` (no ref). Walk wizard, then run.

    CLI flags that have no wizard step (`--post-saved`, `--max-cost`,
    `--self-check`, `--cite-context`, `--no-cache`, `--dry-run`, `--by`,
    `--post-to`) are forwarded as-is so the wizard path matches the direct
    path when the user typed `unread analyze --self-check --post-saved`.
    """
    answers = await _collect_answers(
        mode="analyze",
        console_out=console_out,
        output=output,
        save_default=save_default,
        mark_read=mark_read,
    )
    if answers is None:
        return
    # Wizard's Telegram session is already closed. Dispatching now opens a
    # new one inside whichever command we hand off to.
    if answers.run_on_all_unread:
        from unread.analyzer.commands import run_all_unread_analyze

        await run_all_unread_analyze(
            preset=answers.preset,
            output=answers.output_path,
            console_out=answers.console_out,
            mark_read=answers.mark_read,
            yes=True,  # wizard already confirmed the plan, no second prompt
            folder=answers.run_on_folder,
            language=language,
            report_language=report_language,
            source_language=source_language,
        )
        return

    from unread.analyzer.commands import cmd_analyze

    args = build_analyze_args(answers)
    args["post_saved"] = post_saved
    args["max_cost"] = max_cost
    args["self_check"] = self_check
    args["cite_context"] = cite_context
    args["no_cache"] = no_cache
    args["dry_run"] = dry_run
    args["by"] = by
    args["post_to"] = post_to
    args["language"] = language
    args["report_language"] = report_language
    args["source_language"] = source_language
    # CLI explicit value wins; wizard answer fills in when CLI didn't set it.
    args["with_comments"] = with_comments or bool(answers.with_comments)
    await cmd_analyze(**args)


async def run_interactive_dump(
    *,
    fmt: str = "md",
    output: Path | None = None,
    save_default: bool = False,
    with_transcribe: bool = False,
    include_transcripts: bool = True,
    console_out: bool = False,
    mark_read: bool | None = None,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
) -> None:
    """Default UX for `unread dump` (no ref). Wizard without preset step."""
    answers = await _collect_answers(
        mode="dump",
        console_out=console_out,
        output=output,
        save_default=save_default,
        mark_read=mark_read,
    )
    if answers is None:
        return

    if answers.run_on_all_unread:
        from unread.export.commands import run_all_unread_dump

        await run_all_unread_dump(
            fmt=fmt,
            output=answers.output_path,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=answers.console_out,
            also_save_default=answers.also_save_default,
            mark_read=answers.mark_read,
            yes=True,
            folder=answers.run_on_folder,
            language=language,
            report_language=report_language,
            source_language=source_language,
            **_build_enrich_kwargs(answers),
        )
        return

    from unread.export.commands import cmd_dump

    args = build_dump_args(
        answers,
        fmt=fmt,
        with_transcribe=with_transcribe,
        include_transcripts=include_transcripts,
    )
    args["language"] = language
    args["report_language"] = report_language
    args["source_language"] = source_language
    await cmd_dump(**args)


async def run_interactive_describe() -> None:
    """Default UX for `unread tg describe` (no ref, no filters): pick → show."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        console.print(f"[bold cyan]{i18n_t('wiz_pick_chat_to_describe')}[/]")
        console.print(f"[grey70]{i18n_t('wiz_tips')}[/]\n")
        chat = await _pick_chat(client, offer_all_unread=False)
        if chat is None or chat is ALL_UNREAD:
            console.print(f"[grey70]{i18n_t('cancelled')}[/]")
            return
        chat_ref = str(chat["chat_id"])

    # Now open a fresh session via the existing cmd_describe flow.
    from unread.tg.commands import cmd_describe

    await cmd_describe(chat_ref)


def _period_to_cli_kwargs(answers: InteractiveAnswers) -> dict[str, Any]:
    """Map the wizard's period choice to cmd_ask's CLI kwargs.

    cmd_ask doesn't expose --full-history (use --global instead) or
    --from-msg, so those wizard choices collapse to "no period filter".
    """
    p = answers.period
    if p == "last24h":
        return {"last_hours": 24}
    if p == "last96h":
        return {"last_hours": 96}
    if p == "last7":
        return {"last_days": 7}
    if p == "last30":
        return {"last_days": 30}
    if p == "last90":
        return {"last_days": 90}
    if p == "year_start":
        return {"since": f"{datetime.now(UTC).year}-01-01"}
    if p == "custom":
        return {"since": answers.custom_since, "until": answers.custom_until}
    if p == "from_msg":
        return {}  # ask doesn't honour from_msg
    if p == "full":
        return {}
    return {}  # "unread" or anything else


async def _ensure_tg_for_wizard() -> None:
    """Run the inline-init offer NOW if Telegram isn't ready.

    The wizard's `_collect_answers` opens a Telegram client to drive
    the chat picker; without this preflight, a user with an expired
    session would be asked to type a question (or click through wizard
    steps) only to hit the session-expired prompt at the chat-picker
    step. Doing the offer up-front means the user never types input
    that's about to be thrown away.

    Behavior:
      - Already authorized → no-op.
      - Non-TTY → no-op (let the existing tg_client error path fire,
        same as before; the wizard isn't used in scripted contexts).
      - TTY + missing creds / expired session → offer inline init;
        on decline fall back to the historical exit banners.
    """
    from unread.tg.session_state import is_session_authorized_sync
    from unread.util.prompt import _can_interact

    s = get_settings()
    creds_ok = bool(s.telegram.api_id and s.telegram.api_hash)
    if creds_ok and is_session_authorized_sync(s):
        return
    if not _can_interact():
        return

    from unread.tg.client import (
        _exit_missing_telegram_credentials,
        exit_session_expired,
        offer_inline_tg_init,
    )

    reason = "missing_creds" if not creds_ok else "session_expired"
    if not await offer_inline_tg_init(reason):
        if reason == "missing_creds":
            _exit_missing_telegram_credentials()
        else:
            exit_session_expired()


async def run_interactive_ask(
    *,
    question: str,
    refresh: bool = False,
    semantic: bool = False,
    rerank: bool | None = None,
    limit: int = 200,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    show_retrieved: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
    no_followup: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
    mark_read: bool | None = None,
) -> None:
    """Default UX for `unread ask` (no <ref>, no --chat/--folder/--global).

    Walks the ask-mode wizard, then dispatches to `cmd_ask` with the
    picked scope. The wizard never builds the embeddings index or
    invokes --build-index — that's an explicit user decision.
    """
    # Preflight Telegram BEFORE prompting for the question. Without this,
    # the user types a question, the wizard then opens tg_client at the
    # chat-picker step, and the user has to deal with the session-expired
    # prompt holding a now-pointless typed question. Cheaper to gate up
    # front: ready → continue silently; not ready → offer init now.
    await _ensure_tg_for_wizard()

    # If no question was supplied (bare `unread ask`), prompt for one now.
    # `_collect_answers(mode="ask")` only uses the question for the
    # confirm-step summary; it doesn't ask the user for it.
    # Ctrl-D / Ctrl-C / blank input cancels the run cleanly.
    if not question.strip():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML

        session: PromptSession = PromptSession()
        console.print(f"[bold cyan]{i18n_t('wiz_ask_question_prompt')}[/]")
        try:
            question = (await session.prompt_async(HTML("<ansicyan>?</ansicyan> "))).strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"[grey70]{i18n_t('cancelled')}[/]")
            return
        if not question:
            console.print(f"[grey70]{i18n_t('cancelled')}[/]")
            return

    answers = await _collect_answers(
        mode="ask",
        console_out=console_out,
        output=output,
        save_default=False,
        mark_read=mark_read,
        question=question,
    )
    if answers is None:
        return

    from unread.ask.commands import cmd_ask

    period_kwargs = _period_to_cli_kwargs(answers)

    chat_arg: str | None = None
    if answers.chat_ref:  # non-empty ref → use it
        chat_arg = answers.chat_ref

    # Wizard mode always backfills the picked chat from Telegram before
    # retrieval — the user just stepped through a flow expecting fresh
    # answers, not whatever's stale in the local DB. ALL_LOCAL skips this
    # (no chat list to refresh; explicit local-only path).
    effective_refresh = refresh or chat_arg is not None

    enrich_kwargs = _build_enrich_kwargs(answers)

    # Mark-read is only meaningful when a specific chat is picked. ALL_LOCAL
    # has no single chat to mark, so suppress it there even if the user
    # ticked "yes" (the wizard step is skipped for ALL_LOCAL anyway).
    effective_mark_read: bool | None = None if answers.run_on_all_local else answers.mark_read

    await cmd_ask(
        question=question,
        ref=None,
        chat=chat_arg,
        thread=answers.thread_id,
        folder=None,
        global_scope=answers.run_on_all_local,
        refresh=effective_refresh,
        semantic=semantic,
        rerank=rerank,
        limit=limit,
        model=model,
        output=answers.output_path or output,
        console_out=answers.console_out or console_out,
        show_retrieved=show_retrieved,
        max_cost=max_cost,
        with_comments=bool(answers.with_comments),
        yes=yes,
        no_followup=no_followup,
        language=language,
        report_language=report_language,
        source_language=source_language,
        build_index=False,
        mark_read=effective_mark_read,
        **enrich_kwargs,
        **period_kwargs,
    )


async def _collect_answers(
    *,
    mode: str,  # "analyze" | "dump" | "ask"
    console_out: bool,
    output: Path | None,
    save_default: bool,
    mark_read: bool | None,
    question: str | None = None,
) -> InteractiveAnswers | None:
    """State-machine wizard: each step can go back one without losing context.

    `mode` controls which steps appear:
      - "analyze" walks chat → [comments|thread] → preset → period → enrich → output → mark_read → confirm.
      - "dump" skips preset.
      - "ask" skips preset / output; runs chat → [comments|thread] → period → enrich → mark_read → confirm.
        Ask mode also offers an "ALL synced chats (local DB)" row in the
        chat picker; picking it sets `run_on_all_local=True` and jumps
        straight to period (no thread / comments). ALL_LOCAL also skips
        the mark_read step (no single chat to mark).

    CLI flags pre-fill steps and skip the corresponding prompt:
      - `console_out`, `output`, or `save_default` → skip the output step.
      - `mark_read is not None` → skip the mark-read step.

    `question` is shown in the ask-mode confirm summary so the user can
    sanity-check what they're about to run; not stored on the returned
    `InteractiveAnswers` (callers pass the question through on the side).
    """
    settings = get_settings()
    # Whether the user already made these choices at the CLI. If so, the
    # matching wizard step is suppressed and the forced value is used.
    output_forced = console_out or output is not None or save_default
    mark_read_forced = mark_read is not None

    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        # Make the action ("analyze" / "dump" / etc.) visible up-front; with
        # only "unread dump" in the scrollback, it's easy to forget which command
        # this wizard belongs to by the time you reach the confirm step.
        # Render the banner via i18n; the {action} placeholder is bolded
        # post-format so the value pops without leaking Rich tags into the
        # i18n string.
        banner = i18n_tf("wiz_banner", action=f"[bold]{mode}[/]")
        console.print(f"[bold cyan]{banner}[/]")
        # Show the immutable settings so the user knows what will happen.
        if output_forced:
            out_label = (
                "console (rendered markdown)"
                if console_out
                else (f"{output}" if output is not None else "reports/ (auto-named file)")
            )
            console.print(f"  [grey70]output (from CLI):[/]    [bold]{out_label}[/]")
        if mark_read_forced:
            console.print(f"  [grey70]mark read (from CLI):[/] [bold]{'yes' if mark_read else 'no'}[/]")
        console.print(f"[grey70]{i18n_t('wiz_tips')}[/]\n")

        chat: dict | None = None
        thread_id: int | None = None
        forum_all_flat = False
        forum_all_per_topic = False
        # Authoritative unread count for the picked single topic (from
        # GetForumTopicsRequest). None when no single topic is picked
        # (chat is not a forum, or user picked all-flat / per-topic mode).
        topic_unread: int | None = None
        preset: str | None = None
        enrich_kinds: list[str] | None = None
        period: str | None = None
        custom_since: str | None = None
        custom_until: str | None = None
        custom_from_msg: str | None = None
        with_comments: bool = False
        # Resolved linked-chat id for the picked channel, cached so a
        # back-step doesn't refetch from Telegram.
        linked_chat_id: int | None = None
        # Estimated msg count from the linked discussion chat for the
        # selected analysis window. Filled lazily after the period is
        # picked when with_comments is on; lets the confirm step show
        # `messages ≈ N + ~M comments` and roll comments into the
        # analyze cost estimate.
        comments_count_est: int | None = None
        # Local, step-level state for output + mark_read — start from CLI
        # overrides when present, otherwise get set by the wizard steps.
        chosen_console_out = bool(console_out)
        chosen_output_path: Path | None = output
        # Tracks the "save reports/ + console" wizard option (new dump
        # default). Only ever True alongside `chosen_console_out=True` and
        # `chosen_output_path=None` — see `_pick_output` for the trios.
        chosen_also_save_default = False
        # Default mark-read to True in the wizard for analyze/dump: if the
        # user ran analyze on unread messages they've effectively "seen"
        # them now, so advancing Telegram's read marker matches intent.
        # Ask mode defaults to False — answering a question is often
        # exploratory and shouldn't silently consume the unread state.
        # CLI `--mark-read` / `--no-mark-read` can still override.
        chosen_mark_read = bool(mark_read) if mark_read is not None else (mode != "ask")
        # Per-period message counts for the current chat (filled once we
        # know the chat and, for forums, the thread). Used by `_pick_period`
        # to decorate choices and by the confirm step to estimate cost.
        period_counts: dict[str, int | None] = {}
        # Per-media-kind counts (voice / videonote / video / photo / doc /
        # links / text). For topics, this is filled by the period-step
        # walk (`_fetch_topic_period_counts` returns it for free off the
        # same iteration). For chat-wide scope, the enrich step fills it
        # from `Repo.media_breakdown`. Confirm step renders it on the
        # `enrich:` row so the user sees how much enrichment work each
        # enabled kind will trigger.
        media_counts: dict[str, int] = {}

        run_on_all = False
        run_on_all_local = False
        # When the user picked "Run on a folder…" in the chat step,
        # carries the folder title. Threaded through to InteractiveAnswers
        # so the analyze/dump dispatch can call `run_all_unread_*(folder=...)`.
        run_on_folder_name: str | None = None
        step = "chat"
        while True:
            if step == "chat":
                # Reset flags from any prior chat-step iteration. Without
                # this, picking ALL_UNREAD/ALL_LOCAL, then pressing BACK
                # at a downstream step (e.g. period) and picking a real
                # chat would leave the run-on-all flags stuck True and
                # corrupt the returned answers.
                run_on_all = False
                run_on_all_local = False
                run_on_folder_name = None
                chat = None
                linked_chat_id = None
                # Topic-scoped state from a prior thread step, plus any
                # cached per-period / media counts: a different chat
                # means stale.
                thread_id = None
                topic_unread = None
                period_counts = {}
                media_counts = {}
                comments_count_est = None
                # Ask mode swaps "Run on all N unread" for "Search ALL
                # synced chats (local DB)" — the analyze/dump batch flow
                # doesn't make sense for ask (one question across many
                # chats is the global ALL_LOCAL path).
                result = await _pick_chat(
                    client,
                    offer_all_unread=(mode != "ask"),
                    offer_all_local=(mode == "ask"),
                )
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                if result is ALL_UNREAD:
                    run_on_all = True
                    # Still let the user pick a preset for analyze; skip
                    # everything else (thread/period/custom-range) — batch
                    # is always "each chat's own unread".
                    step = (
                        "preset"
                        if mode == "analyze"
                        else _next_step_after_mark_read(output_forced, mark_read_forced)
                    )
                    continue
                if isinstance(result, _PickedFolder):
                    # Folder batch: same downstream flow as ALL_UNREAD,
                    # narrowed to chats in `result.name`. The folder
                    # name flows into InteractiveAnswers and on to
                    # `run_all_unread_*(folder=...)`.
                    run_on_all = True
                    run_on_folder_name = result.name
                    step = (
                        "preset"
                        if mode == "analyze"
                        else _next_step_after_mark_read(output_forced, mark_read_forced)
                    )
                    continue
                if result is ALL_LOCAL:
                    # Ask mode only: skip thread/comments and go straight
                    # to period over the global local corpus.
                    run_on_all_local = True
                    step = "period"
                    continue
                chat = result
                # Channels can carry a linked discussion group (comments).
                # Detect once now so the next step ("comments") can offer
                # the toggle. Look up from DB first; fall back to Telegram
                # only if the row hasn't recorded the link yet. Best-effort:
                # any failure leaves linked_chat_id=None and the comments
                # step is skipped.
                linked_chat_id = None
                if chat["kind"] == "channel":
                    try:
                        from unread.tg.topics import get_linked_chat_id

                        async with open_repo(get_settings().storage.data_path) as _r:
                            row = await _r.get_chat(int(chat["chat_id"]))
                        linked_chat_id = (row or {}).get("linked_chat_id")
                        if linked_chat_id is None:
                            linked_chat_id = await get_linked_chat_id(client, int(chat["chat_id"]))
                            if linked_chat_id is not None:
                                async with open_repo(get_settings().storage.data_path) as _r:
                                    await _r.upsert_chat(
                                        int(chat["chat_id"]),
                                        "channel",
                                        title=chat.get("title"),
                                        username=chat.get("username"),
                                        linked_chat_id=linked_chat_id,
                                    )
                    except Exception as e:
                        log.debug("interactive.linked_lookup_failed", err=str(e)[:200])
                        linked_chat_id = None
                if chat["kind"] == "forum":
                    step = "thread"
                elif chat["kind"] == "channel" and linked_chat_id is not None:
                    step = "comments"
                elif mode == "analyze":
                    step = "preset"
                else:
                    # Ask and dump: no preset step. Go to period so the
                    # user can pick a date range before enrich/confirm.
                    step = "period"

            elif step == "comments":
                # Channel-only step: include the linked discussion group's
                # messages? Asked once per chat selection. Default Yes
                # (the user opted into a channel — comments are usually
                # the more interesting part).
                result = await _bind_escape(
                    questionary.select(
                        i18n_t("wiz_include_comments_q"),
                        choices=[
                            questionary.Choice(i18n_t("wiz_yes_with_comments"), value=True),
                            questionary.Choice(i18n_t("wiz_no_only_posts"), value=False),
                            questionary.Choice(i18n_t("wiz_back"), value=BACK),
                        ],
                        style=LIST_STYLE,
                    ),
                    BACK,
                ).ask_async()
                if result is BACK:
                    step = "chat"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                with_comments = bool(result)
                step = "preset" if mode == "analyze" else "period"

            elif step == "thread":
                result = await _pick_thread(client, chat["chat_id"])
                if result is BACK:
                    step = "chat"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                thread_id, forum_all_flat, forum_all_per_topic, topic_unread = result
                # Different topic → different per-period and media counts.
                # Force a refetch on the next period / enrich step.
                period_counts = {}
                media_counts = {}
                step = "preset" if mode == "analyze" else "period"

            elif step == "preset":
                # Only runs for analyze mode.
                result = await _pick_preset()
                if result is BACK:
                    if run_on_all:
                        step = "chat"
                    elif chat and chat["kind"] == "forum":
                        step = "thread"
                    elif chat and chat["kind"] == "channel" and linked_chat_id is not None:
                        step = "comments"
                    else:
                        step = "chat"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                preset = result
                # Analyze run-on-all skips period+enrich, and now also
                # the output step (default = save+print). Go straight to
                # mark_read or confirm.
                step = ("confirm" if mark_read_forced else "mark_read") if run_on_all else "period"

            elif step == "period":
                # Lazily fetch per-period counts once we know chat+thread.
                # ALL_LOCAL (ask mode) has no chat scope, so skip the
                # per-chat count fetch — counts are simply absent.
                if not period_counts and chat is not None:
                    if thread_id is not None:
                        # Single-topic scope: msg_ids interleave across
                        # topics in a forum, so the chat-level
                        # `_fetch_period_counts` approximation is wrong
                        # here. `topic_unread` is authoritative (from
                        # GetForumTopicsRequest); periods come from a
                        # capped iteration over the topic's messages.
                        # The same walk also classifies each message's
                        # media so we can show per-kind counts on the
                        # `enrich:` row at confirm time without an
                        # extra DB round-trip.
                        period_counts, media_counts = await _fetch_topic_period_counts(
                            client,
                            chat_id=int(chat["chat_id"]),
                            thread_id=thread_id,
                            topic_unread=int(topic_unread or 0),
                        )
                    else:
                        # Whole-chat scope (incl. forum all-flat / all-per-topic
                        # modes where thread_id is None). `unread_hint`
                        # comes from the dialog picker (chat object).
                        unread_hint = int(chat.get("unread") or 0)
                        period_counts = await _fetch_period_counts(
                            client,
                            chat_id=int(chat["chat_id"]),
                            thread_id=None,
                            unread_hint=unread_hint,
                        )
                result = await _pick_period(counts=period_counts)
                if result is BACK:
                    # Period sits between `preset` (analyze) / chat-or-thread
                    # (dump) and `enrich`. Mirror the pre-existing back-step
                    # logic that used to live on the enrich block.
                    if mode == "analyze":
                        step = "preset"
                    elif chat and chat["kind"] == "forum":
                        step = "thread"
                    elif (
                        mode in ("ask", "dump")
                        and chat
                        and chat["kind"] == "channel"
                        and linked_chat_id is not None
                    ):
                        step = "comments"
                    else:
                        # Includes ask + ALL_LOCAL (run_on_all_local) and
                        # ask + private/group chats: back to chat picker.
                        step = "chat"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                period, custom_since, custom_until, custom_from_msg = result
                # For a custom date range, estimate the message count on
                # demand so the confirm step can show "messages ≈ N" and
                # the cost estimate (analyze mode) has a number to work
                # with. Cheap: 2 get_messages(limit=1) calls.
                if period == "custom" and chat is not None and (custom_since or custom_until):
                    # Pre-prod fix: build tz-aware UTC datetimes here too,
                    # mirroring `_period_to_db_filters` at :252-254. The
                    # naive datetimes that lived here previously got
                    # interpreted as local time by Telethon's offset_date
                    # parameter, skewing the confirm-screen count by the
                    # host's TZ offset (a user in NZST seeing yesterday's
                    # messages counted under today's date).
                    _since_dt = (
                        datetime.strptime(custom_since, "%Y-%m-%d").replace(tzinfo=UTC)
                        if custom_since
                        else None
                    )
                    _until_dt = (
                        datetime.strptime(custom_until, "%Y-%m-%d").replace(tzinfo=UTC)
                        if custom_until
                        else None
                    )
                    if thread_id is not None:
                        # Same reasoning as the period-counts dispatch
                        # above: msg_id arithmetic is chat-wide, so use
                        # iteration for a single topic.
                        period_counts["custom"] = await _count_custom_range_topic(
                            client,
                            chat_id=int(chat["chat_id"]),
                            thread_id=thread_id,
                            since=_since_dt,
                            until=_until_dt,
                        )
                    else:
                        period_counts["custom"] = await _count_custom_range(
                            client,
                            chat_id=int(chat["chat_id"]),
                            thread_id=None,
                            since=_since_dt,
                            until=_until_dt,
                        )
                # When comments are on for a channel, estimate the linked
                # discussion's message count for the same window so the
                # plan reflects the real workload. The actual run pulls
                # comments by date range bounded by the channel posts'
                # date span; we approximate that here per the chosen
                # period. Best-effort — None on lookup failure.
                if with_comments and linked_chat_id is not None and chat is not None and period:
                    comments_count_est = await _estimate_comments_count(
                        client,
                        channel_chat=chat,
                        linked_chat_id=linked_chat_id,
                        period=period,
                        custom_since=custom_since,
                        custom_until=custom_until,
                    )
                # Ask mode skips output/mark_read but keeps enrich (so
                # voice/image/link content becomes searchable mid-flow).
                # Exception: ALL_LOCAL (run_on_all_local) skips enrich
                # because cmd_ask refuses to enrich every synced chat —
                # showing the picker would mislead the user.
                step = "confirm" if mode == "ask" and run_on_all_local else "enrich"

            elif step == "enrich":
                # Runs for both analyze and dump so media-to-text
                # conversion flows into either output path. Counts are
                # period-scoped: filter the local DB by the time/msg-id
                # window the user just picked so "(N in db)" reflects
                # what the run will actually process.
                #
                # For a single forum topic, the period step's walk
                # already classified every message's media (see
                # `_fetch_topic_period_counts`) and populated
                # `media_counts` for free — and from Telegram, not the
                # DB, so it isn't gated on whether the topic has been
                # synced. Skip the DB round-trip in that case.
                if chat is not None and not media_counts:
                    breakdown_kwargs = _period_to_db_filters(
                        period=period,
                        custom_since=custom_since,
                        custom_until=custom_until,
                        custom_from_msg=custom_from_msg,
                        chat=chat,
                    )
                    try:
                        async with open_repo(get_settings().storage.data_path) as repo:
                            media_counts = await repo.media_breakdown(
                                int(chat["chat_id"]),
                                thread_id=thread_id,
                                **breakdown_kwargs,
                            )
                    except Exception as e:
                        log.debug("interactive.media_breakdown_failed", err=str(e)[:200])
                if media_counts.get("total"):
                    parts: list[str] = []
                    if media_counts.get("any_media"):
                        parts.append(i18n_tf("wiz_plan_with_media", n=media_counts["any_media"]))
                    if media_counts.get("links"):
                        parts.append(i18n_tf("wiz_plan_with_urls", n=media_counts["links"]))
                    extras = ", " + ", ".join(parts) if parts else ""
                    console.print(
                        f"[grey70]"
                        f"{i18n_tf('wiz_plan_for_period_synced', total=media_counts['total'], extras=extras)}"
                        f"[/]"
                    )
                result = await _pick_enrich(media_counts=media_counts)
                if result is BACK:
                    step = "period"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                enrich_kinds = list(result) if isinstance(result, list) else None
                # Ask mode skips output but still asks about mark-read when
                # a single chat is picked (ALL_LOCAL is a no-op since
                # there's no single chat to mark, and CLI passing
                # --mark-read/--no-mark-read also suppresses the step).
                if mode == "ask":
                    step = "confirm" if (run_on_all_local or mark_read_forced) else "mark_read"
                elif mode == "analyze":
                    # Analyze defaults to "save to reports/ + print to
                    # terminal" — both happen unconditionally now, so the
                    # wizard's output step is redundant. CLI flags
                    # (--output / --no-save) still pre-fill via
                    # output_forced and bypass any wizard prompting; their
                    # values are already in chosen_console_out /
                    # chosen_output_path from the initial assignment.
                    step = "confirm" if mark_read_forced else "mark_read"
                else:
                    step = "mark_read" if output_forced else "output"

            elif step == "output":
                result = await _pick_output(
                    default_path=output,
                )
                if result is BACK:
                    # Order: chat → thread → preset → period → enrich → output
                    # Run-on-all skips period+enrich, so it backs to preset
                    # (analyze) or chat (dump).
                    step = "enrich" if not run_on_all else ("preset" if mode == "analyze" else "chat")
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                chosen_console_out, chosen_output_path, chosen_also_save_default = result
                step = "confirm" if mark_read_forced else "mark_read"

            elif step == "mark_read":
                result = await _pick_mark_read(default=chosen_mark_read)
                if result is BACK:
                    if mode == "ask":
                        # Ask mode goes enrich → mark_read → confirm
                        # (skipping output entirely). BACK from mark_read
                        # returns to enrich.
                        step = "enrich"
                    elif mode == "analyze":
                        # Analyze also skips the output step now (default
                        # is save+print). BACK from mark_read returns to
                        # enrich (or preset for run-on-all where period+
                        # enrich are skipped).
                        step = "enrich" if not run_on_all else "preset"
                    elif output_forced:
                        # Dump with CLI override → no output step → go back
                        # to enrich (or chat for run-on-all where period+
                        # enrich are skipped).
                        step = "enrich" if not run_on_all else "chat"
                    else:
                        step = "output"
                    continue
                if result is None:
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                chosen_mark_read = bool(result)
                step = "confirm"

            elif step == "confirm":
                # Header line: action + chat scope (chat title, with
                # forum topic id / mode qualifier appended). Lead with
                # the action (analyze / dump / ask) so the user doesn't
                # have to scroll up to remember which command they ran.
                if run_on_all:
                    if run_on_folder_name:
                        header_value = i18n_tf("wiz_plan_folder_chats", folder=run_on_folder_name)
                    else:
                        header_value = i18n_t("wiz_plan_all_unread_chats")
                elif run_on_all_local:
                    # Ask mode "search ALL synced chats" path. Re-uses
                    # the picker label for consistency with the chat-
                    # step echo.
                    header_value = i18n_t("wiz_ask_all_local")
                else:
                    header_value = chat.get("title") or str(chat["chat_id"])
                    if thread_id:
                        header_value += f" / {i18n_tf('wiz_plan_topic', id=thread_id)}"
                    elif forum_all_flat:
                        header_value += f" / {i18n_t('wiz_plan_all_flat')}"
                    elif forum_all_per_topic:
                        header_value += f" / {i18n_t('wiz_plan_per_topic')}"
                console.print(f"[bold]{i18n_t('wiz_plan_label')} ([yellow]{mode}[/]):[/] {header_value}")

                # Body rows: collected as (label, value) pairs and
                # printed with aligned label widths. The ENRICH row
                # carries per-kind counts ("voice 8 · video 3 · …")
                # built from `media_counts` so the user sees the
                # actual workload before committing.
                rows: list[tuple[str, str]] = []
                if mode == "analyze" and preset:
                    rows.append((i18n_t("wiz_summary_step_preset"), str(preset)))
                if not run_on_all and not run_on_all_local:
                    rows.append(
                        (
                            i18n_t("wiz_summary_step_period"),
                            _format_period_for_plan(
                                period,
                                custom_since,
                                custom_until,
                                custom_from_msg,
                                period_counts,
                            ),
                        )
                    )
                # Enrichment is asked for analyze / dump / ask (all
                # three walk the enrich step), but not the run-on-all
                # variants where it's auto.
                if enrich_kinds is not None and not run_on_all and not run_on_all_local:
                    rows.append(
                        (
                            i18n_t("wiz_summary_step_enrich"),
                            _format_enrich_for_plan(enrich_kinds, media_counts),
                        )
                    )
                # Output row: not shown for ask (no save step).
                if mode != "ask":
                    if chosen_console_out and chosen_also_save_default:
                        output_value = i18n_t("wiz_plan_save_reports_and_console")
                    elif chosen_console_out:
                        output_value = i18n_t("wiz_plan_console")
                    elif chosen_output_path:
                        output_value = str(chosen_output_path)
                    else:
                        output_value = i18n_t("wiz_plan_save_reports")
                    rows.append((i18n_t("wiz_summary_step_output"), output_value))
                # mark-read row: shown for analyze / dump and for ask
                # in single-chat scope. ALL_LOCAL ask hides it because
                # there's no single chat to mark.
                show_mark_read = chosen_mark_read and (mode != "ask" or not run_on_all_local)
                if show_mark_read:
                    rows.append(
                        (
                            i18n_t("wiz_summary_step_mark_read"),
                            i18n_t("wiz_summary_yes"),
                        )
                    )
                # Question (ask mode only): shown last so it reads as
                # the natural-language tail of the plan.
                if mode == "ask" and question:
                    rows.append((i18n_t("wiz_plan_question_label"), str(question)))

                if rows:
                    label_w = max(len(label) for label, _ in rows)
                    for label, value in rows:
                        # `+ 1` for the trailing colon; we ljust the
                        # plain text so Rich's render width math isn't
                        # confused by markup inside `value`.
                        padded = f"{label}:".ljust(label_w + 1)
                        console.print(f"  [grey70]{padded}[/] {value}")

                # Only show a cost estimate for the analyze flow (dump
                # doesn't hit OpenAI for chat completion) and when we have
                # a concrete count.
                if mode == "analyze" and not run_on_all and preset is not None:
                    n_channel = _count_for_period(period, period_counts)
                    # When comments are on, fold the linked-chat estimate
                    # into the messages tally so the cost estimate covers
                    # the full workload — both the channel posts and the
                    # discussion replies are sent through the analyzer.
                    if n_channel is not None and with_comments and comments_count_est is not None:
                        n_msgs = n_channel + comments_count_est
                    else:
                        n_msgs = n_channel
                    if n_msgs is not None and n_msgs > 0:
                        wizard_presets = get_presets(
                            settings.locale.report_language or settings.locale.language or "en"
                        )
                        chosen_preset = (
                            wizard_presets.get(preset)
                            or wizard_presets.get("summary")
                            or next(iter(wizard_presets.values()), None)
                        )
                        if chosen_preset is None:
                            cost_lo = cost_hi = None
                        else:
                            cost_lo, cost_hi = _estimate_cost(
                                n_messages=n_msgs,
                                preset=chosen_preset,
                                settings=settings,
                            )
                        # If comments are folded in, render the breakdown
                        # so the user sees where the count comes from
                        # (e.g. "537 + ~5234 comments").
                        msg_count_str = _format_msg_count_with_comments(
                            n_channel,
                            comments_count_est if with_comments else None,
                        )
                        if cost_lo is None:
                            console.print(
                                f"  [grey70]{i18n_t('wiz_plan_msgs_approx')}[/] {msg_count_str}  "
                                f"[grey70]{i18n_t('wiz_plan_pricing_missing')}[/]"
                            )
                        else:
                            console.print(
                                f"  [grey70]{i18n_t('wiz_plan_msgs_approx')}[/] {msg_count_str}  "
                                f"[grey70]{i18n_t('wiz_plan_cost_approx')}[/] "
                                f"{_fmt_cost_range(cost_lo, cost_hi)}  "
                                f"[grey70]{i18n_t('wiz_plan_analysis_estimate')}[/]"
                            )
                        # The analysis estimate doesn't include enrichment
                        # costs. When media_counts is populated (topic walk
                        # or DB breakdown), compute the actual per-kind
                        # estimate; otherwise fall back to the rate-list
                        # hint so users still see the order-of-magnitude.
                        extra_kinds = _extra_enrich_kinds(enrich_kinds)
                        enrich_cost_est = _estimate_enrich_cost(enrich_kinds, media_counts)
                        if enrich_cost_est is not None and enrich_kinds:
                            console.print(
                                f"  [dim yellow]{i18n_t('wiz_plan_enrich_cost_label')}[/] "
                                f"{_fmt_cost(enrich_cost_est)}  "
                                f"[grey70]{i18n_t('wiz_plan_enrich_cost_assumptions')}[/] "
                                "[cyan]unread stats[/]"
                                f"[grey70]{i18n_t('wiz_plan_extra_enrich_hint_close')}[/]"
                            )
                        elif extra_kinds:
                            console.print(
                                f"  [dim yellow]{i18n_t('wiz_plan_extra_enrich_label')}[/] "
                                f"[yellow]{', '.join(extra_kinds)}[/] "
                                f"[grey70]{i18n_t('wiz_plan_extra_enrich_hint')}[/] "
                                "[cyan]unread stats[/]"
                                f"[grey70]{i18n_t('wiz_plan_extra_enrich_hint_close')}[/]"
                            )
                    elif n_msgs == 0:
                        console.print(f"  [yellow]{i18n_t('wiz_plan_zero_msgs')}[/]")
                elif mode == "dump" and not run_on_all:
                    n_channel = _count_for_period(period, period_counts)
                    if n_channel is not None:
                        msg_count_str = _format_msg_count_with_comments(
                            n_channel,
                            comments_count_est if with_comments else None,
                        )
                        console.print(
                            f"  [grey70]{i18n_t('wiz_plan_msgs_approx')}[/] {msg_count_str}  "
                            f"[grey70]{i18n_t('wiz_plan_dump_free')}[/]"
                        )

                choice = await _bind_escape(
                    questionary.select(
                        i18n_t("wiz_run_it_q"),
                        choices=[
                            questionary.Choice(i18n_t("wiz_run_choice"), value="run"),
                            questionary.Choice(i18n_t("wiz_back"), value=BACK),
                            questionary.Choice(i18n_t("wiz_cancel_choice"), value="cancel"),
                        ],
                        style=LIST_STYLE,
                    ),
                    BACK,
                ).ask_async()
                if choice is None or choice == "cancel":
                    console.print(f"[grey70]{i18n_t('cancelled')}[/]")
                    return None
                if choice is BACK:
                    if mode == "ask":
                        # Ask mode walks period → enrich → mark_read →
                        # confirm (output step always skipped). ALL_LOCAL
                        # skips both enrich and mark_read so it backs all
                        # the way to period. CLI --mark-read/--no-mark-read
                        # suppresses the mark_read step (back to enrich).
                        if run_on_all_local:
                            step = "period"
                        elif mark_read_forced:
                            step = "enrich"
                        else:
                            step = "mark_read"
                    elif mark_read_forced:
                        step = (
                            "output"
                            if not output_forced
                            else ("period" if not run_on_all else ("preset" if mode == "analyze" else "chat"))
                        )
                    else:
                        step = "mark_read"
                    continue
                break

        # Ask mode keeps `preset` as None (no preset step ran). For
        # analyze/dump the legacy fallback to "summary" stays — dump
        # ignores the value, and analyze always sets it before reaching
        # this point anyway.
        if mode == "ask":
            final_preset: str | None = preset  # always None today
        else:
            final_preset = preset if preset is not None else "summary"
        no_chat_scope = run_on_all or run_on_all_local
        return InteractiveAnswers(
            chat_ref="" if no_chat_scope else str(chat["chat_id"]),
            chat_kind="" if no_chat_scope else chat["kind"],
            thread_id=thread_id,
            forum_all_flat=forum_all_flat,
            forum_all_per_topic=forum_all_per_topic,
            preset=final_preset,
            period=period if period is not None else "unread",
            custom_since=custom_since,
            custom_until=custom_until,
            console_out=chosen_console_out,
            mark_read=chosen_mark_read,
            output_path=chosen_output_path,
            also_save_default=chosen_also_save_default,
            run_on_all_unread=run_on_all,
            run_on_all_local=run_on_all_local,
            run_on_folder=run_on_folder_name,
            enrich_kinds=enrich_kinds,
            custom_from_msg=custom_from_msg,
            with_comments=with_comments,
        )


def _next_step_after_mark_read(output_forced: bool, mark_read_forced: bool) -> str:
    """Pick the next step when skipping period (run-on-all-unread path).

    Both output and mark-read steps can be skipped via CLI flags; this
    consolidates the branching so the state machine stays readable.
    """
    if not output_forced:
        return "output"
    if not mark_read_forced:
        return "mark_read"
    return "confirm"


# -------------------------------------------------- per-period counts + cost


def _count_for_period(period: str | None, counts: dict[str, int | None]) -> int | None:
    """Look up the prefetched count for the currently picked period.

    `custom` is populated on-demand by `_collect_answers` after the user
    submits their date range (via `_count_custom_range`); if that
    computation failed or the user hasn't provided dates yet, the entry
    is absent and we return None.
    """
    if period is None:
        return None
    return counts.get(period)


async def _fetch_period_counts(
    client,
    *,
    chat_id: int,
    thread_id: int | None,
    unread_hint: int,
) -> dict[str, int | None]:
    """Estimate message counts covering each canonical period.

    **Approximation by msg_id difference.** We tried two RPCs — `GetHistory`
    and `SearchRequest` — and both returned the chat-wide total
    regardless of `offset_date` / `min_date` for this kind of peer on
    this Telethon/server combo. The reliable workaround is:

      `count_in_period ≈ latest_msg.id − first_msg_after_period_start.id + 1`

    Telegram assigns `msg_id` sequentially per chat, so the id difference
    is close to the real count. It over-estimates slightly when deletions
    or service messages left gaps, under-estimates if Telegram has
    renumbered (rare). For the picker's "let me see how much work I'm
    signing up for" purpose, order-of-magnitude is what matters.

    Two lightweight `get_messages(limit=1, ...)` calls per period:
      - one to find the oldest msg at/after the period start,
      - one (shared across periods) to find the latest msg.
    Errors drop to None; the picker renders that as "—".
    """
    now = datetime.now(UTC)

    thread_kw: dict = {"reply_to": thread_id} if thread_id else {}

    async def _latest_msg_id() -> int | None:
        try:
            msgs = await client.get_messages(chat_id, limit=1, **thread_kw)
            if not msgs:
                return None
            return int(msgs[0].id)
        except Exception as e:
            log.debug("period_counts.latest_fail", chat_id=chat_id, err=str(e)[:200])
            return None

    async def _first_id_after(since: datetime | None) -> int | None:
        """First msg_id at/after `since`. None means 'find the first msg
        in the chat at all' → used for full history."""
        try:
            if since is None:
                # Oldest message in the chat/topic: reverse=True + no
                # offset_date returns messages in ascending order starting
                # from the first one.
                msgs = await client.get_messages(chat_id, limit=1, reverse=True, **thread_kw)
            else:
                # First message at or after `since`: reverse=True says
                # "ascending order", offset_date excludes messages older
                # than `since`.
                msgs = await client.get_messages(
                    chat_id, limit=1, offset_date=since, reverse=True, **thread_kw
                )
            if not msgs:
                return None
            return int(msgs[0].id)
        except Exception as e:
            log.debug("period_counts.first_fail", chat_id=chat_id, since=str(since), err=str(e)[:200])
            return None

    latest_task = _latest_msg_id()
    last24h_start_task = _first_id_after(now - timedelta(hours=24))
    last96h_start_task = _first_id_after(now - timedelta(hours=96))
    last7_start_task = _first_id_after(now - timedelta(days=7))
    last30_start_task = _first_id_after(now - timedelta(days=30))
    last90_start_task = _first_id_after(now - timedelta(days=90))
    year_start_task = _first_id_after(datetime(now.year, 1, 1, tzinfo=UTC))
    full_start_task = _first_id_after(None)

    (
        latest,
        last24h_start,
        last96h_start,
        last7_start,
        last30_start,
        last90_start,
        year_start_id,
        full_start,
    ) = await _asyncio.gather(
        latest_task,
        last24h_start_task,
        last96h_start_task,
        last7_start_task,
        last30_start_task,
        last90_start_task,
        year_start_task,
        full_start_task,
    )

    def _count(start: int | None) -> int | None:
        if latest is None or start is None:
            return None
        return max(0, latest - start + 1)

    out: dict[str, int | None] = {
        "last24h": _count(last24h_start),
        "last96h": _count(last96h_start),
        "last7": _count(last7_start),
        "last30": _count(last30_start),
        "last90": _count(last90_start),
        "year_start": _count(year_start_id),
        "full": _count(full_start),
        # For "unread" we already have a hint from the dialog row.
        "unread": unread_hint if unread_hint else None,
    }
    # Sanity clamps. Periods can't exceed full; unread can't exceed the
    # period it falls inside (best-effort — unread_hint is server-authoritative
    # and trumps our approximation, so we only clamp the other direction).
    if out.get("full") is not None:
        for key in ("last24h", "last96h", "last7", "last30", "last90", "year_start"):
            if out[key] is not None and out[key] > out["full"]:
                out[key] = out["full"]
    if out.get("unread") is not None and out.get("full") is not None and out["unread"] > out["full"]:
        out["unread"] = out["full"]
    return out


async def _count_custom_range(
    client,
    *,
    chat_id: int,
    thread_id: int | None,
    since: datetime | None,
    until: datetime | None,
) -> int | None:
    """Estimate messages in [since, until] via msg_id difference.

    Same technique as `_fetch_period_counts`, specialized for a
    user-picked date range at the wizard's custom step. Two
    `get_messages(limit=1, ...)` calls:
      - newest at/before `until` (or latest if until is None);
      - oldest at/after `since` (or very oldest if since is None).
    Returns None on failure so the caller falls back to rendering "—".
    """
    thread_kw: dict = {"reply_to": thread_id} if thread_id else {}

    try:
        # Upper bound: latest msg_id whose date is ≤ until.
        if until is None:
            upper = await client.get_messages(chat_id, limit=1, **thread_kw)
        else:
            upper = await client.get_messages(chat_id, limit=1, offset_date=until, **thread_kw)
        if not upper:
            return 0
        upper_id = int(upper[0].id)

        # Lower bound: earliest msg_id whose date is ≥ since.
        if since is None:
            lower = await client.get_messages(chat_id, limit=1, reverse=True, **thread_kw)
        else:
            lower = await client.get_messages(chat_id, limit=1, offset_date=since, reverse=True, **thread_kw)
        if not lower:
            return 0
        lower_id = int(lower[0].id)

        return max(0, upper_id - lower_id + 1)
    except Exception as e:
        log.debug(
            "custom_count.error",
            chat_id=chat_id,
            since=str(since),
            until=str(until),
            err=str(e)[:200],
        )
        return None


# Cap on per-topic message walks. Five sequential GetReplies pages at
# Telethon's default chunk_size of 100 — adds ~1-2s of latency at the
# period-picker step in the worst case (busy topic). Periods that extend
# past the oldest walked message are returned as None ("—").
_TOPIC_COUNT_CAP = 500


def _classify_walk_message(msg) -> dict[str, bool]:
    """Per-message media flags matching `Repo.media_breakdown`'s schema.

    Used by `_fetch_topic_period_counts` to tally voice / videonote /
    video / photo / doc / links / text counts during the same walk that
    produces period buckets — so the confirm step can render per-kind
    enrichment scope without an extra DB round-trip and without
    requiring the topic to be already synced.

    Key names mirror `media_breakdown`: singulars for media kinds
    (`voice`, `videonote`, `video`, `photo`, `doc`) and plural `links` /
    `text`. Mapping at the call site — wizard's enrich kind `image`
    maps to `photo`, `link` to `links`.
    """
    flags: dict[str, bool] = {
        "voice": False,
        "videonote": False,
        "video": False,
        "photo": False,
        "doc": False,
        "links": False,
        "text": False,
    }
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if text:
        flags["text"] = True

    media = getattr(msg, "media", None)
    if media is not None:
        # Lazy-import Telethon types to keep module-load cost off the
        # CLI's hot path (`unread --help` etc.).
        from telethon.tl.types import (
            DocumentAttributeAudio,
            DocumentAttributeVideo,
            MessageMediaDocument,
            MessageMediaPhoto,
            MessageMediaWebPage,
        )

        if isinstance(media, MessageMediaPhoto):
            flags["photo"] = True
        elif isinstance(media, MessageMediaDocument):
            doc = getattr(media, "document", None)
            attrs = getattr(doc, "attributes", []) or []
            voice = video = round_msg = False
            for a in attrs:
                if isinstance(a, DocumentAttributeAudio) and getattr(a, "voice", False):
                    voice = True
                elif isinstance(a, DocumentAttributeVideo):
                    video = True
                    if getattr(a, "round_message", False):
                        round_msg = True
            # Round-message video notes win over plain video; voice over
            # generic audio. Anything else is a doc.
            if round_msg:
                flags["videonote"] = True
            elif voice:
                flags["voice"] = True
            elif video:
                flags["video"] = True
            else:
                flags["doc"] = True
        elif isinstance(media, MessageMediaWebPage):
            flags["links"] = True

    if not flags["links"]:
        entities = getattr(msg, "entities", None) or []
        if entities:
            try:
                from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

                for e in entities:
                    if isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)):
                        flags["links"] = True
                        break
            except Exception:  # pragma: no cover — defensive only
                pass
    # Fallback heuristic — matches `media_breakdown`'s `text LIKE '%http%'`
    # so that plain-text URLs (no entity, no preview) still register.
    if not flags["links"] and "http" in text:
        flags["links"] = True

    return flags


async def _fetch_topic_period_counts(
    client,
    *,
    chat_id: int,
    thread_id: int,
    topic_unread: int,
) -> tuple[dict[str, int | None], dict[str, int]]:
    """Per-period message counts AND per-kind media tally for a topic.

    `_fetch_period_counts`'s msg_id-difference trick (`latest - first + 1`)
    is chat-wide because Telegram assigns msg_ids sequentially per chat,
    not per topic — for a topic, the difference counts every message
    between the topic's bounds *across all topics*, which is wildly
    wrong (often the chat's whole unread total).

    Workaround: walk the topic's messages once with `iter_messages
    (reply_to=thread_id)` up to `_TOPIC_COUNT_CAP` and bucket each by
    date. `topic_unread` is authoritative (from `GetForumTopicsRequest`)
    and used directly for the "unread" row.

    The same walk also classifies each message's media via
    `_classify_walk_message`, returning a media tally that the confirm
    step renders on the `enrich:` row. Saturated walks under-count both
    period and media tallies — periods detect this via the boundary-vs-
    oldest-walked check; media counts are reported as a lower bound
    (the user is already informed by the saturated period buckets).

    Cost: roughly `cap / 100` sequential RPCs at the period-picker step.
    Periods whose start is older than the oldest walked message are
    returned as None so the picker shows "—" instead of a guess.
    """
    now = datetime.now(UTC)
    boundaries: list[tuple[str, datetime]] = [
        ("last24h", now - timedelta(hours=24)),
        ("last96h", now - timedelta(hours=96)),
        ("last7", now - timedelta(days=7)),
        ("last30", now - timedelta(days=30)),
        ("last90", now - timedelta(days=90)),
        ("year_start", datetime(now.year, 1, 1, tzinfo=UTC)),
    ]
    counts: dict[str, int] = {key: 0 for key, _ in boundaries}
    media: dict[str, int] = {
        "voice": 0,
        "videonote": 0,
        "video": 0,
        "photo": 0,
        "doc": 0,
        "links": 0,
        "text": 0,
        "any_media": 0,
    }
    walked = 0
    oldest: datetime | None = None

    try:
        async for msg in client.iter_messages(chat_id, reply_to=thread_id, limit=_TOPIC_COUNT_CAP):
            walked += 1
            md = getattr(msg, "date", None)
            if md is None:
                continue
            oldest = md
            for key, boundary in boundaries:
                if md >= boundary:
                    counts[key] += 1
            flags = _classify_walk_message(msg)
            for key, present in flags.items():
                if present:
                    media[key] += 1
            if any(flags[k] for k in ("voice", "videonote", "video", "photo", "doc")):
                media["any_media"] += 1
    except Exception as e:
        log.debug(
            "topic_period_counts.error",
            chat_id=chat_id,
            thread_id=thread_id,
            err=str(e)[:200],
        )
        out_err: dict[str, int | None] = {key: None for key, _ in boundaries}
        out_err["full"] = None
        out_err["unread"] = topic_unread if topic_unread else None
        # Media counts are best-effort: return whatever we collected
        # before the error (zeros if the iteration never started).
        media["total"] = walked
        return out_err, media

    saturated = walked >= _TOPIC_COUNT_CAP
    out: dict[str, int | None] = {}
    for key, boundary in boundaries:
        if saturated and oldest is not None and oldest > boundary:
            # We hit the cap before reaching this period's start, so the
            # bucket is an undercount. Return None instead of misleading.
            out[key] = None
        else:
            out[key] = counts[key]
    # `full` is the total topic message count; we only know it exactly
    # when the walk wasn't truncated.
    out["full"] = None if saturated else walked
    out["unread"] = topic_unread if topic_unread else None
    # Mirror `media_breakdown`'s `total` so downstream code can treat
    # the dict the same way.
    media["total"] = walked
    return out, media


async def _estimate_comments_count(
    client,
    *,
    channel_chat: dict,
    linked_chat_id: int,
    period: str,
    custom_since: str | None,
    custom_until: str | None,
) -> int | None:
    """Estimate the linked discussion's msg count for the analysis window.

    Comments are pulled at run time by date range — bounded either by
    the user-picked period (when explicit) or by the date span of the
    channel's pulled posts (when period == "unread"/"from_msg"/"full"
    and `since/until` aren't set upstream). This helper mirrors that
    logic on a best-effort basis so the wizard's confirm step can show
    `messages ≈ N + ~M comments` instead of hiding the comments work.

    Strategy by period:
      - canonical date periods (`last24h` … `year_start`, `custom`)
        → known `since`/`until` → cheap msg-id arithmetic on the
          linked chat (`_count_custom_range`, two `get_messages` calls).
      - `unread` → look up the date of the oldest unread channel msg
        (one extra `get_messages(min_id=read_inbox_max_id, reverse=True)`
        call), then arithmetic on the linked chat from that date.
      - `full` → no lower bound; estimate the linked chat's full
        history.
      - `from_msg` → would need to resolve the ref to a msg id and
        then a date; skip (returns None) — uncommon path, the actual
        run still works, just no estimate on the plan.

    Returns None when the window can't be derived or the lookup fails.
    """
    now = datetime.now(UTC)
    since: datetime | None = None
    until: datetime | None = None

    if period == "last24h":
        since = now - timedelta(hours=24)
    elif period == "last96h":
        since = now - timedelta(hours=96)
    elif period == "last7":
        since = now - timedelta(days=7)
    elif period == "last30":
        since = now - timedelta(days=30)
    elif period == "last90":
        since = now - timedelta(days=90)
    elif period == "year_start":
        since = datetime(now.year, 1, 1, tzinfo=UTC)
    elif period == "custom":
        if custom_since:
            since = datetime.strptime(custom_since, "%Y-%m-%d").replace(tzinfo=UTC)
        if custom_until:
            until = datetime.strptime(custom_until, "%Y-%m-%d").replace(tzinfo=UTC)
    elif period == "unread":
        # Date of the oldest unread channel msg → comments since then.
        # Without a read marker (fresh channel sub) we have no
        # derivable lower bound — better to return None than to
        # silently estimate the linked chat's full history, which
        # would be wildly inflated.
        read_max = int(channel_chat.get("read_inbox_max_id") or 0)
        if not read_max:
            return None
        try:
            msgs = await client.get_messages(
                int(channel_chat["chat_id"]),
                limit=1,
                min_id=read_max,
                reverse=True,
            )
        except Exception as e:
            log.debug("comments_estimate.unread_lookup_failed", err=str(e)[:200])
            return None
        if not msgs or not getattr(msgs[0], "date", None):
            # Read marker exists but no msgs after it → nothing unread
            # → no comments to estimate.
            return None
        since = msgs[0].date
    elif period == "full":
        pass  # since=None → linked chat's full history
    else:
        # `from_msg` and any future periods we haven't taught about
        # land here — skip rather than guess.
        return None

    # Telethon's `get_messages(int_chat_id, ...)` only works when the
    # entity is in the session's `entity_cache`. The cache is populated
    # by `iter_dialogs` (which the chat picker ran), but a linked
    # *discussion* group the user has never explicitly opened may not
    # be there — Telethon then raises `ValueError: Could not find the
    # input entity for ...`, `_count_custom_range`'s try/except swallows
    # it, and we get a silent `None`. Resolve the entity once first so
    # subsequent calls hit the cached value.
    try:
        await client.get_input_entity(linked_chat_id)
    except Exception as e:
        log.debug(
            "comments_estimate.entity_resolve_failed",
            linked_chat_id=linked_chat_id,
            err=str(e)[:200],
        )
        return None

    return await _count_custom_range(
        client,
        chat_id=linked_chat_id,
        thread_id=None,
        since=since,
        until=until,
    )


async def _count_custom_range_topic(
    client,
    *,
    chat_id: int,
    thread_id: int,
    since: datetime | None,
    until: datetime | None,
) -> int | None:
    """Iteration-based message count for a single topic in [since, until].

    Same reason `_fetch_topic_period_counts` exists: msg_id arithmetic
    is chat-wide and meaningless for a topic. Walks newest-first from
    `until` (or latest), stops as soon as `since` is crossed, and caps
    at `_TOPIC_COUNT_CAP`. Returns None when the cap is hit (the user
    can still proceed; the confirm step will just show "—").
    """
    iter_kwargs: dict = {"reply_to": thread_id, "limit": _TOPIC_COUNT_CAP}
    if until is not None:
        iter_kwargs["offset_date"] = until

    walked = 0
    count = 0
    try:
        async for msg in client.iter_messages(chat_id, **iter_kwargs):
            walked += 1
            md = getattr(msg, "date", None)
            if md is None:
                continue
            if since is not None and md < since:
                # Crossed the lower bound — exact count.
                return count
            count += 1
        if walked >= _TOPIC_COUNT_CAP:
            # Cap hit before reaching `since` (or the topic's start);
            # we have a lower bound but not the truth.
            return None
        return count
    except Exception as e:
        log.debug(
            "custom_count_topic.error",
            chat_id=chat_id,
            thread_id=thread_id,
            since=str(since),
            until=str(until),
            err=str(e)[:200],
        )
        return None


def _estimate_cost(
    *,
    n_messages: int,
    preset: Preset,
    settings,
) -> tuple[float | None, float | None]:
    """Wizard-side wrapper for `analyzer.pipeline.estimate_cost`.

    Kept as a private alias so the legacy call sites in this module
    don't churn; the canonical implementation lives in pipeline so
    `cmd_analyze`'s `--max-cost` guard can reuse it.
    """
    from unread.analyzer.pipeline import estimate_cost as _est

    return _est(n_messages=n_messages, preset=preset, settings=settings)


def _fmt_count(n: int) -> str:
    """Right-align a count in `_COL_UNREAD`-char field; dim em-dash if zero."""
    return f"{n:>{_COL_UNREAD}}" if n else f"{'—':>{_COL_UNREAD}}"


def _fmt_cost(value: float | None) -> str:
    """Cost-aware formatter: keep sub-cent values visible.

    `${v:.2f}` rounds anything under half a cent to "$0.00" — useless
    for small chats where a summary is genuinely $0.003. Scale precision
    to the magnitude instead.
    """
    if value is None:
        return "—"
    if value <= 0:
        return "$0"
    if value < 0.001:
        return "< $0.001"
    if value < 0.01:
        return f"${value:.4f}"  # $0.0045
    if value < 1.0:
        return f"${value:.3f}"  # $0.023
    return f"${value:.2f}"


def _extra_enrich_kinds(kinds: list[str] | None) -> list[str]:
    """Return enrichment kinds that cost extra beyond the defaults.

    voice + videonote are on by default in config.toml and produce
    small, predictable audio cost — the user knows about those. The
    point of this notice is the spendier additions: video, image, doc,
    link. `None` means "wizard wasn't used / defaults" → no notice.
    """
    if kinds is None:
        return []
    extras = [k for k in kinds if k not in ("voice", "videonote")]
    return extras


# Per-unit enrichment cost estimates. Audio rates assume Whisper at
# $0.006/min; voice/videonote default to a 30s average duration (most
# voice notes), video to 60s (typical short clip). These are rough —
# the real cost is recorded per-call in `usage_log` and surfaced by
# `unread stats`. See `wiz_plan_enrich_cost_assumptions` (i18n) for
# the user-facing caveat copy.
_ENRICH_AVG_VOICE_MIN = 0.5
_ENRICH_AVG_VIDEONOTE_MIN = 0.5
_ENRICH_AVG_VIDEO_MIN = 1.0
_WHISPER_USD_PER_MIN = 0.006
_VISION_USD_PER_PHOTO = 0.0002
_LINK_USD_PER_URL = 0.0001

_ENRICH_PER_UNIT_USD: dict[str, float] = {
    "voice": _ENRICH_AVG_VOICE_MIN * _WHISPER_USD_PER_MIN,
    "videonote": _ENRICH_AVG_VIDEONOTE_MIN * _WHISPER_USD_PER_MIN,
    "video": _ENRICH_AVG_VIDEO_MIN * _WHISPER_USD_PER_MIN,
    "image": _VISION_USD_PER_PHOTO,
    "link": _LINK_USD_PER_URL,
    # `doc` extracts text without an LLM call (PDF/DOCX parsing is
    # local) — no per-unit charge here. The downstream analyzer pays
    # the usual chat-completion cost on the extracted text, which is
    # already covered by the messages-based estimate.
    "doc": 0.0,
}


def _estimate_enrich_cost(
    enrich_kinds: list[str] | None,
    media_counts: dict[str, int] | None,
) -> float | None:
    """Rough total enrichment cost from per-kind counts × per-unit rates.

    Returns None when there's nothing to estimate (no enrich kinds, or
    no media counts available — e.g. ALL_LOCAL ask scope where no chat
    is selected). Returns 0.0 when enrichment is enabled but every
    enabled kind has zero matching messages.

    Wizard-name → count-key mapping mirrors `_format_enrich_for_plan`
    (`image` → `photo`, `link` → `links`).
    """
    if not enrich_kinds or not media_counts:
        return None
    total = 0.0
    for kind in enrich_kinds:
        count_key = _ENRICH_KIND_TO_COUNT_KEY.get(kind, kind)
        n = media_counts.get(count_key, 0)
        total += n * _ENRICH_PER_UNIT_USD.get(kind, 0.0)
    return total


def _fmt_cost_range(lo: float | None, hi: float | None) -> str:
    """Render a (lo, hi) cost range; collapse to one number if they're close."""
    if lo is None and hi is None:
        return "—"
    if lo is None or hi is None or abs((hi or 0) - (lo or 0)) < 1e-4:
        return _fmt_cost(lo if lo is not None else hi)
    return f"{_fmt_cost(lo)}–{_fmt_cost(hi)}"


# Maps the wizard's enrich-kind names to `media_counts` dict keys.
# `image` → photos in DB / Telethon; `link` → text-link tally. The
# remaining kinds (`voice`, `videonote`, `video`, `doc`) match by name.
_ENRICH_KIND_TO_COUNT_KEY: dict[str, str] = {
    "image": "photo",
    "link": "links",
}


def _format_enrich_for_plan(
    enrich_kinds: list[str],
    media_counts: dict[str, int],
) -> str:
    """Render the `enrich:` row value: kinds with their counts.

    Each enabled kind shows as `name N` (e.g. `voice 8`) when
    `media_counts` has data for it; bare `name` if the count is
    unknown (no walk + DB miss). Kinds with `0` stay visible so the
    user can spot enabled-but-empty kinds rather than silently
    dropping them.

    `_ENRICH_KIND_TO_COUNT_KEY` handles the wizard-name → count-key
    mismatches (`image` ↔ `photo`, `link` ↔ `links`).
    """
    if not enrich_kinds:
        return i18n_t("wiz_plan_enrich_none_value")
    parts: list[str] = []
    for kind in enrich_kinds:
        count_key = _ENRICH_KIND_TO_COUNT_KEY.get(kind, kind)
        count = media_counts.get(count_key) if media_counts else None
        if count is None:
            parts.append(kind)
        else:
            parts.append(f"{kind} {count}")
    return " · ".join(parts)


def _format_msg_count_with_comments(
    n_channel: int | None,
    comments_count: int | None,
) -> str:
    """Render the messages-line count for the confirm step.

    Plain integer when no comments are folded in; `N + ~M comments`
    when the wizard has a comments estimate to attach. The `~`
    indicates that the comments count is an upper-bound msg-id-arithmetic
    approximation (same as the chat-wide period counts), not an exact
    fetch — keeps the user from being surprised when the actual run
    pulls a slightly different number.
    """
    if n_channel is None:
        return "—"
    if comments_count is None or comments_count <= 0:
        return str(n_channel)
    return f"{n_channel} + ~{comments_count} comments"


def _format_period_for_plan(
    period: str | None,
    custom_since: str | None,
    custom_until: str | None,
    custom_from_msg: str | None,
    period_counts: dict[str, int | None] | None,
) -> str:
    """Render the `period:` row value for the multiline confirm step.

    Bare period code for the canonical periods (`unread`, `last7`, …);
    code + bracketed range for `custom` (with the estimated message
    count if we have one); code + bracketed message-id ref for
    `from_msg`. Mirrors the inline-summary logic that used to live
    in the confirm block before the multiline split.
    """
    if not period:
        return "—"
    if period == "custom":
        rng = f"{custom_since or ''}..{custom_until or ''}"
        n = period_counts.get("custom") if period_counts else None
        if n is not None:
            return f"{period} ({rng}; ≈{n})"
        return f"{period} ({rng})"
    if period == "from_msg" and custom_from_msg:
        return f"{period} (from {custom_from_msg})"
    return period


def _fmt_date(dt: datetime | None) -> str:
    """Compact date for picker rows.

    Returns a short string (no padding) — the caller right-pads to the
    column width. Rules:
      - today → `HH:MM` (5 chars)
      - this year → `MMM DD HH:MM` (12 chars, e.g. `Apr 23 09:14`)
      - older → `YYYY-MM-DD` (10 chars)
      - missing → `—` (1 char, caller pads)

    Telethon datetimes are tz-aware UTC; we convert to system local before
    display so "last msg" matches what the Telegram app shows the user.
    Naive datetimes pass through unchanged (assumed already local).
    """
    if dt is None:
        return "—"
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # → system local
        now = datetime.now().astimezone()
    else:
        now = datetime.now()
    delta_s = (now - dt).total_seconds()
    if -60 < delta_s < 24 * 3600:
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%b %d %H:%M")
    return dt.strftime("%Y-%m-%d")


# Canonical short label for each Telegram kind — keeps the `kind` column
# narrow without losing meaning. Basic groups and supergroups are the
# same thing from the user's perspective in this picker, so both map to
# "group"; the underlying value on the dialog stays unchanged so callers
# that actually care (forum routing) still get the precise kind.
_KIND_SHORT = {
    "supergroup": "group",
    "group": "group",
    "channel": "channel",
    "forum": "forum",
    "user": "user",
}


def _short_kind(kind: str) -> str:
    return _KIND_SHORT.get(kind, kind)


# Column widths — chosen empirically so the most common values fit
# without padding waste. Titles trail the fixed columns so their
# variable width doesn't misalign anything.
_COL_UNREAD = 6  # up to 999999 unread — plenty
_COL_KIND = 7  # "channel" is the longest after shortening
_COL_DATE = 12  # "Apr 23 09:14" is the longest short form
_COL_FOLDER = 14  # truncated to keep the title column readable on 80-col terms


def _fmt_folder(folders: list[str] | None) -> str:
    """Render a chat's folder membership for the picker.

    Multiple folders are joined with `+`; the whole field is truncated to
    `_COL_FOLDER` so a chat in many folders doesn't push the title off
    the right edge. Empty cell when the chat is in no folder (very common).
    """
    if not folders:
        return ""
    s = "+".join(folders)
    if len(s) > _COL_FOLDER:
        return s[: _COL_FOLDER - 1] + "…"
    return s


def _chat_row(
    *,
    unread: int,
    kind: str,
    last_msg_date: datetime | None,
    title: str | int | None,
    folders: list[str] | None = None,
    subscribed: bool = False,
) -> str:
    """One formatted row in the chat-picker table.

    Two spaces between columns (not `·`) — the aligned whitespace reads
    as columns on its own, and dots just add visual noise at typical
    terminal widths. Title is unpadded (trails everything).

    `subscribed=True` prefixes the title with a star so users browsing
    `unread tg chats add` see at a glance which dialogs they've already
    subscribed to (and avoid duplicate-adding).
    """
    star = "★ " if subscribed else "  "
    return (
        f"{_fmt_count(unread)}  "
        f"{_short_kind(kind):<{_COL_KIND}}  "
        f"{_fmt_date(last_msg_date):<{_COL_DATE}}  "
        f"{_fmt_folder(folders):<{_COL_FOLDER}}  "
        f"{star}{title or ''}"
    )


def _chat_header_row() -> str:
    # The 2-char gap before the title matches the `★ `/`  ` slot rows
    # carry so the header label still lines up over the title column.
    return (
        f"{i18n_t('wiz_col_unread'):>{_COL_UNREAD}}  "
        f"{i18n_t('wiz_col_kind'):<{_COL_KIND}}  "
        f"{i18n_t('wiz_col_last_msg'):<{_COL_DATE}}  "
        f"{i18n_t('wiz_col_folder'):<{_COL_FOLDER}}  "
        f"  {i18n_t('wiz_col_title')}"
    )


async def _pick_chat(
    client,
    *,
    offer_all_unread: bool = False,
    offer_all_local: bool = False,
    subscribed_ids: set[int] | None = None,
) -> dict | None | object:
    """Show dialogs with unread (sorted by count desc), offer all-dialogs fallback.

    `subscribed_ids`, when provided, prefixes each row whose chat_id is
    in the set with a `★` so the user can see what's already in
    `unread tg chats list` without leaving the picker. Empty set / None
    behaves like before.

    `offer_all_local` (used by the ask wizard) adds a "Search ALL synced
    chats (local DB)" row that returns the `ALL_LOCAL` sentinel —
    triggers the no-scope local-only path (zero TG round-trips).

    Returns one of:
      - dict (picked chat) — a resolved entry
      - ALL_UNREAD — user picked "Run on all N unread chats" (if offer_all_unread)
      - _PickedFolder(name=...) — user picked "Run on a folder…" and a folder (if offer_all_unread)
      - ALL_LOCAL — user picked "Search ALL synced chats" (if offer_all_local)
      - None — cancelled
    """
    from unread.tg.folders import chat_folder_index

    unread = await list_unread_dialogs(client)
    sub_set = subscribed_ids or set()

    if not unread:
        console.print(f"[yellow]{i18n_t('wiz_no_unread_showing_all')}[/]")
        return await _pick_from_all(
            client,
            subscribed_ids=sub_set,
            offer_all_local=offer_all_local,
        )

    # Folder index: empty dict if the user hasn't defined any folders or
    # the lookup fails (we don't want to abort the picker over a side-info
    # column).
    try:
        folder_idx = await chat_folder_index(client)
    except Exception as e:
        log.warning("interactive.folder_index_failed", err=str(e)[:200])
        folder_idx = {}

    # Top of the list: navigation actions (search-all, run-all-unread) so
    # the highlight starts on the first one and user reaches them without
    # scrolling. Per-chat rows follow under their column header.
    choices: list[Any] = []
    choices.append(
        questionary.Choice(
            title=i18n_t("wiz_search_all_dialogs"),
            value=("all", None),
        )
    )
    if offer_all_local:
        choices.append(
            questionary.Choice(
                title=i18n_t("wiz_ask_all_local"),
                value=ALL_LOCAL,
            )
        )
    if offer_all_unread:
        total = sum(d.unread_count for d in unread)
        choices.append(
            questionary.Choice(
                title=i18n_tf("wiz_run_on_all_unread", n=len(unread), total=total),
                value=("all_unread", None),
            )
        )
        # Folder batch sits right under "Run on ALL" — same family of
        # whole-batch actions, just narrower scope. Only meaningful for
        # analyze/dump (offer_all_unread=True), not ask mode.
        choices.append(
            questionary.Choice(
                title=i18n_t("wiz_run_on_folder"),
                value=("folder", None),
            )
        )
    choices.append(questionary.Separator())
    choices.append(questionary.Separator(_chat_header_row()))
    first_chat_choice: questionary.Choice | None = None
    for d in unread:
        c = questionary.Choice(
            title=_chat_row(
                unread=d.unread_count,
                kind=d.kind,
                last_msg_date=d.last_msg_date,
                title=d.title or d.chat_id,
                folders=folder_idx.get(d.chat_id),
                subscribed=d.chat_id in sub_set,
            ),
            value=("pick", d),
        )
        choices.append(c)
        if first_chat_choice is None:
            first_chat_choice = c
    # No "Back" on the first step — there's nowhere to go back to.
    # Ctrl-C cancels the whole wizard.

    # Default highlight on the first chat row (not the search button) so
    # Enter immediately drills into the top-unread chat — the most common
    # action. The search/run-all/folder buttons stay above for fast access
    # via arrow-up.
    result = await _bind_escape(
        questionary.select(
            i18n_tf("wiz_pick_chat_n_unread", n=len(unread)),
            choices=choices,
            default=first_chat_choice,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=i18n_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        None,
    ).ask_async()

    if result is None:
        return None
    # ALL_LOCAL row returns the sentinel object directly (not a tuple) —
    # short-circuit before the tuple-unpack so the ask wizard's no-scope
    # path is reachable without breaking the existing per-chat / all /
    # all_unread shape.
    if result is ALL_LOCAL:
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: [bold]{i18n_t('wiz_ask_all_local')}[/]"
        )
        return ALL_LOCAL
    action, payload = result
    if action == "all_unread":
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: "
            f"[bold]{i18n_t('wiz_summary_chat_all_unread')}[/]"
        )
        return ALL_UNREAD
    if action == "folder":
        picked_name = await _pick_folder(client)
        if picked_name is None:
            # Folder picker cancelled (or no folders exist) — fall back
            # to the chat picker so the user can pick something else
            # without restarting the wizard.
            return await _pick_chat(
                client,
                offer_all_unread=offer_all_unread,
                offer_all_local=offer_all_local,
                subscribed_ids=sub_set,
            )
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: "
            f"[bold]{i18n_tf('wiz_plan_folder_chats', folder=picked_name)}[/]"
        )
        return _PickedFolder(name=picked_name)
    if action == "all":
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: "
            f"[grey70]{i18n_t('wiz_summary_chat_searching_all')}[/]"
        )
        return await _pick_from_all(client, subscribed_ids=sub_set)
    d = payload
    _replace_last_line(
        f"[bold cyan]?[/] chat: [bold]{d.title or d.chat_id}[/] [grey70]({d.kind}, {d.unread_count} unread)[/]"
    )
    return {
        "chat_id": d.chat_id,
        "kind": d.kind,
        "title": d.title,
        "username": d.username,
        "read_inbox_max_id": d.read_inbox_max_id,
        "unread": d.unread_count,
    }


async def _pick_folder(client) -> str | None:
    """Show the user's Telegram folders and return the picked folder title.

    Returns None on cancel or when the account has no folders. The
    caller (`_pick_chat`) re-opens the chat picker on None so users
    don't get stuck in a dead end. We compute per-folder unread / chat
    counts from the same dialog snapshot used by the chat picker so
    the user sees how much work each folder represents before picking.
    """
    from unread.tg.dialogs import list_unread_dialogs
    from unread.tg.folders import list_folders

    try:
        folders = await list_folders(client)
    except Exception as e:
        log.warning("interactive.list_folders_failed", err=str(e)[:200])
        folders = []
    if not folders:
        console.print(f"[yellow]{i18n_t('wiz_no_folders')}[/]")
        return None

    # Per-folder unread tally from the unread-dialogs snapshot.
    # `list_unread_dialogs` only returns dialogs with unread > 0, which
    # is exactly what `--folder` batches over. Total chats = explicit
    # include count from the folder definition (may include read chats);
    # unread is what `run_all_unread_analyze` will actually process.
    try:
        unread_dialogs = await list_unread_dialogs(client)
    except Exception as e:
        log.debug("interactive.list_unread_for_folders_failed", err=str(e)[:200])
        unread_dialogs = []
    unread_by_chat = {d.chat_id: d.unread_count for d in unread_dialogs}

    choices: list[Any] = []
    for f in folders:
        unread_in_folder = sum(unread_by_chat.get(cid, 0) for cid in f.include_chat_ids)
        emoji = (f.emoticon + " ") if f.emoticon else ""
        meta = i18n_tf(
            "wiz_folder_unread_chats",
            unread=unread_in_folder,
            total=len(f.include_chat_ids),
        )
        choices.append(
            questionary.Choice(
                title=f"{emoji}{f.title}  ({meta})",
                value=f.title,
            )
        )
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title=i18n_t("wiz_back"), value=BACK))

    result = await _bind_escape(
        questionary.select(
            i18n_tf("wiz_pick_folder_q", n=len(folders)),
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=i18n_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()

    if result is None or result is BACK:
        return None
    return str(result)


async def _pick_from_all(
    client,
    *,
    subscribed_ids: set[int] | None = None,
    offer_all_local: bool = False,
) -> dict | None | object:
    """Scan every dialog and present a searchable list.

    `subscribed_ids` prefixes each subscribed row with a `★` so the
    `unread tg chats add` flow shows what's already on the subscription list.

    `offer_all_local` (used by the ask wizard) prepends a "Search ALL
    synced chats (local DB)" row that returns the `ALL_LOCAL` sentinel.
    Honouring it here matters for the zero-unread fallback: `_pick_chat`
    short-circuits to this helper when there are no unread dialogs, and
    that's exactly when ask-wizard users most want the option.
    """
    from unread.tg.client import _chat_kind, entity_id, entity_title, entity_username
    from unread.tg.dialogs import UnreadDialog, correct_forum_unread

    # Build UnreadDialog rows so we can run the same forum-count correction
    # the unread-only picker uses. Without this, forums show garbage like
    # 99,755 unread (Telegram caps the dialog-level counter at 99,999 and
    # rarely decrements it on partial mark-read).
    snapshot: list[UnreadDialog] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = d.entity
        snapshot.append(
            UnreadDialog(
                chat_id=entity_id(entity),
                kind=_chat_kind(entity),
                title=entity_title(entity),
                username=entity_username(entity),
                unread_count=int(getattr(d, "unread_count", 0) or 0),
                read_inbox_max_id=int(getattr(d, "read_inbox_max_id", 0) or 0),
            )
        )
    await correct_forum_unread(client, snapshot)
    from unread.tg.folders import chat_folder_index

    try:
        folder_idx = await chat_folder_index(client)
    except Exception as e:
        log.warning("interactive.folder_index_failed", err=str(e)[:200])
        folder_idx = {}
    rows: list[dict] = [
        {
            "chat_id": d.chat_id,
            "kind": d.kind,
            "title": d.title,
            "username": d.username,
            "unread": d.unread_count,
            "read_inbox_max_id": d.read_inbox_max_id,
            "folders": folder_idx.get(d.chat_id, []),
        }
        for d in snapshot
    ]
    if not rows:
        console.print(f"[yellow]{i18n_t('wiz_no_dialogs_at_all')}[/]")
        return None

    # Unread chats first (by count desc, then alpha) — keeps triage on top.
    # Read chats below, grouped by kind (channel → group → forum → user)
    # then alpha within each bucket — easier to scan a long list of already-
    # read dialogs when bots/users aren't interleaved with channels.
    _kind_order = {"channel": 0, "supergroup": 1, "group": 1, "forum": 2, "user": 3}
    rows.sort(
        key=lambda r: (
            0 if r["unread"] > 0 else 1,
            -r["unread"] if r["unread"] > 0 else _kind_order.get(r["kind"], 99),
            (r["title"] or "").lower(),
        )
    )

    # Mirror the shape of _pick_chat's table so navigation between the two
    # pickers isn't visually jarring. Last-msg-date isn't available from
    # iter_dialogs here without extra fetches — leave it blank.
    sub_set = subscribed_ids or set()
    header_line = f"{'unread':>{_COL_UNREAD}}  {'kind':<{_COL_KIND}}  {'folder':<{_COL_FOLDER}}    title"
    choices: list[Any] = []
    # ALL_LOCAL sits above the table so the ask-wizard's no-scope path
    # is reachable here too — see the zero-unread fallback in _pick_chat.
    if offer_all_local:
        choices.append(
            questionary.Choice(
                title=i18n_t("wiz_ask_all_local"),
                value=ALL_LOCAL,
            )
        )
        choices.append(questionary.Separator())
    choices.append(questionary.Separator(header_line))
    choices.extend(
        questionary.Choice(
            title=(
                f"{_fmt_count(r['unread'])}  "
                f"{_short_kind(r['kind']):<{_COL_KIND}}  "
                f"{_fmt_folder(r['folders']):<{_COL_FOLDER}}  "
                f"{'★ ' if r['chat_id'] in sub_set else '  '}"
                f"{r['title'] or r['chat_id']}"
            ),
            value=r,
        )
        for r in rows
    )

    picked = await _bind_escape(
        questionary.select(
            i18n_tf("wiz_pick_chat_n", n=len(rows)),
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=i18n_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        None,
    ).ask_async()
    # ALL_LOCAL is a sentinel object — short-circuit before the dict
    # access so the ask wizard's no-scope path is reachable from here.
    if picked is ALL_LOCAL:
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: [bold]{i18n_t('wiz_ask_all_local')}[/]"
        )
        return ALL_LOCAL
    if picked is not None:
        _replace_last_line(
            f"[bold cyan]?[/] chat: [bold]{picked['title'] or picked['chat_id']}[/] "
            f"[grey70]({picked['kind']}"
            + (f", {picked['unread']} unread" if picked["unread"] else "")
            + ")[/]"
        )
    return picked


async def _pick_thread(client, chat_id: int):
    """Return (thread_id, all_flat, all_per_topic, topic_unread), BACK, or None (cancelled).

    `topic_unread` is the picked topic's unread message count (from
    GetForumTopicsRequest). It is None when the user picks a forum-mode
    option (all-flat / per-topic) instead of a single topic — those
    modes operate over the whole forum, so the chat-level unread count
    is the right hint at the period step.
    """
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print(f"[yellow]{i18n_t('no_topics_in_forum')}[/]")
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
            i18n_tf("wiz_n_topics_in_forum", n=len(topics)),
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=i18n_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        ("back", None),
    ).ask_async()

    if result is None:
        return None
    action, payload = result
    if action == "back":
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    mode_label = i18n_t("wiz_summary_step_mode")
    if action == "per_topic":
        _replace_last_line(
            f"[bold cyan]?[/] {mode_label}: [bold]{i18n_t('wiz_summary_mode_per_topic')}[/] "
            f"[grey70]{i18n_t('wiz_summary_mode_per_topic_hint')}[/]"
        )
        return None, False, True, None
    if action == "flat":
        _replace_last_line(
            f"[bold cyan]?[/] {mode_label}: [bold]{i18n_t('wiz_summary_mode_all_flat')}[/] "
            f"[grey70]{i18n_t('wiz_summary_mode_all_flat_hint')}[/]"
        )
        return None, True, False, None
    picked_topic = next((t for t in topics_sorted if t.topic_id == payload), None)
    label = picked_topic.title if picked_topic else str(payload)
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_topic')}: [bold]{label}[/]")
    topic_unread = picked_topic.unread_count if picked_topic else None
    return payload, False, False, topic_unread


async def _pick_preset():
    """Returns preset name (str), BACK, or None.

    Reads presets from `presets/<active-language>/`. Each preset's
    `description` frontmatter field is shown next to its name; new
    presets are picked up automatically (drop a `.md` into the language
    directory with `description:` set).

    Presets with `hidden: true` in frontmatter are filtered out — those
    are auto-selected by routing logic (`single_msg`, `multichat`,
    `video`, `website`) and showing them in the picker would be noise.
    The CLI's `--preset` flag still accepts them by name.

    The `preferred` list orders the visible presets by popularity (the
    sequence a typical user reaches for first); user-authored custom
    presets land after, sorted alphabetically.
    """
    from unread.config import get_settings

    locale = get_settings().locale
    active_lang = (locale.report_language or locale.language or "en").lower()
    presets_for_lang = {
        name: preset for name, preset in get_presets(active_lang).items() if not preset.hidden
    }

    preferred = [
        "summary",
        "tldr",
        "digest",
        "highlights",
        "quotes",
        "links",
        "action_items",
        "decisions",
        "questions",
        "reactions",
    ]
    names = [p for p in preferred if p in presets_for_lang]
    names += [n for n in sorted(presets_for_lang.keys()) if n not in preferred]

    def _label(name: str) -> str:
        preset = presets_for_lang[name]
        desc = preset.description or preset.prompt_version
        return f"{name:<13} — {desc}"

    choices: list[Any] = [questionary.Choice(title=_label(n), value=n) for n in names]
    choices.append(questionary.Separator())
    choices.append(questionary.Choice(title=i18n_t("wiz_back"), value=BACK))

    picked = await _bind_escape(
        questionary.select(
            i18n_t("wiz_pick_preset_q"),
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction=i18n_t("wiz_filter_instruction"),
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()
    if picked is None:
        return None
    if picked is BACK:
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_preset')}: [bold]{picked}[/]")
    return picked


async def _pick_output(*, default_path: Path | None):
    """Returns (console_out, output_path, also_save_default), BACK, or None.

    `default_path` seeds the custom-path prompt so the user can edit an
    already-provided value instead of retyping it.

    `also_save_default=True` only goes with `console_out=True` and
    `output_path=None` — it signals "render to terminal AND save to default
    reports/", the new wizard default for dump.
    """
    choices = [
        questionary.Choice(i18n_t("wiz_output_save_and_console"), value=("both", None)),
        questionary.Choice(i18n_t("wiz_output_save_default"), value=("file", None)),
        questionary.Choice(i18n_t("wiz_output_save_custom"), value=("custom", None)),
        questionary.Choice(i18n_t("wiz_output_console"), value=("console", None)),
        questionary.Separator(),
        questionary.Choice(i18n_t("wiz_back"), value=(BACK, None)),
    ]
    picked = await _bind_escape(
        questionary.select(
            i18n_t("wiz_output_q"),
            choices=choices,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        (BACK, None),
    ).ask_async()
    if picked is None:
        return None
    action, _ = picked
    if action is BACK:
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    out_label = i18n_t("wiz_summary_step_output")
    if action == "both":
        _replace_last_line(
            f"[bold cyan]?[/] {out_label}: [bold]{i18n_t('wiz_plan_save_reports_and_console')}[/]"
        )
        return True, None, True
    if action == "console":
        _replace_last_line(f"[bold cyan]?[/] {out_label}: [bold]{i18n_t('wiz_summary_step_console')}[/]")
        return True, None, False
    if action == "file":
        _replace_last_line(
            f"[bold cyan]?[/] {out_label}: [bold]{i18n_t('wiz_summary_step_reports_dir')}[/] "
            f"[grey70]{i18n_t('wiz_summary_step_auto_named')}[/]"
        )
        return False, None, False
    # Custom path — prompt for the exact path.
    seed = str(default_path) if default_path else ""
    raw = await questionary.text(
        i18n_t("wiz_output_custom_prompt"),
        default=seed,
    ).ask_async()
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        # User cleared the field — treat as cancel of this sub-step and
        # bounce back to the picker.
        return await _pick_output(default_path=default_path)
    path = Path(raw).expanduser()
    _replace_last_line(f"[bold cyan]?[/] {out_label}: [bold]{path}[/]")
    return False, path, False


async def _pick_enrich(*, media_counts: dict[str, int] | None = None) -> list[str] | None | object:
    """Pick which media kinds to enrich this run.

    Returns a list of enabled kind names (possibly empty = "none"),
    BACK to step back, or None to cancel. Pre-checks the current config
    defaults so the common case is "hit Enter".

    `media_counts` decorates each row with how many messages of that kind
    are already in the local DB for this chat — gives the user a feel for
    cost before committing to enrichment. Pass `None` to skip the
    decoration (used by tests / first-run flows).
    """
    settings = get_settings()
    cfg = settings.enrich
    counts = media_counts or {}

    def _suffix(kind_db_key: str) -> str:
        # `link` doesn't map to a media_type — proxy via "messages with a
        # URL substring" which media_breakdown precomputes.
        n = counts.get(kind_db_key, 0)
        return f"  ({i18n_tf('wiz_enrich_in_db', n=n)})" if n else ""

    # Order reflects default-on status: voice + videonote first (the two
    # kinds that default to True in config), then the opt-in enrichments.
    # Keeping default-on items at the top means hitting Enter through the
    # wizard mostly picks the pre-checked defaults and the user sees what's
    # on without scrolling.
    all_kinds = [
        ("voice", f"{i18n_t('wiz_enrich_voice')}{_suffix('voice')}", cfg.voice),
        (
            "videonote",
            f"{i18n_t('wiz_enrich_videonote')}{_suffix('videonote')}",
            cfg.videonote,
        ),
        ("link", f"{i18n_t('wiz_enrich_link')}{_suffix('links')}", cfg.link),
        ("video", f"{i18n_t('wiz_enrich_video')}{_suffix('video')}", cfg.video),
        (
            "image",
            f"{i18n_t('wiz_enrich_image')}{_suffix('photo')}",
            cfg.image,
        ),
        ("doc", f"{i18n_t('wiz_enrich_doc')}{_suffix('doc')}", cfg.doc),
    ]
    choices = [
        questionary.Choice(title=label, value=key, checked=default_on) for key, label, default_on in all_kinds
    ]
    picked = await _bind_escape(
        _bind_arrow_checkbox(
            questionary.checkbox(
                i18n_t("wiz_enrich_q"),
                choices=choices,
                style=LIST_STYLE,
            )
        ),
        BACK,
    ).ask_async()
    if picked is None:
        return None
    if picked is BACK:
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    summary = ",".join(picked) if picked else i18n_t("wiz_enrich_summary_none")
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_enrich')}: [bold]{summary}[/]")
    return picked


async def _prompt_msg_ref() -> str | None:
    """Collect a Telegram message reference (link or numeric id).

    Returns the raw string (cmd_analyze's `_parse_from_msg` does the actual
    parsing — we only pre-flight validate so the user catches typos in the
    wizard instead of after a 10-minute backfill). Returns None if the user
    cancels via ESC/Ctrl-C or leaves the field blank.
    """
    from unread.tg.links import parse as _parse_link

    while True:
        raw = await questionary.text(
            i18n_t("wiz_msg_ref_prompt"),
            default="",
        ).ask_async()
        if raw is None:
            return None  # Ctrl-C
        raw = raw.strip()
        if not raw:
            return None  # user cleared the field
        # Shortcut: bare numeric id (with optional leading -). cmd_analyze
        # would accept this too, but validating here saves a round trip.
        if raw.lstrip("-").isdigit():
            return raw
        # Otherwise treat as a Telegram link and confirm it carries a msg_id.
        try:
            parsed = _parse_link(raw)
        except Exception as e:
            console.print(f"[red]{i18n_tf('wiz_msg_ref_cant_parse', raw=raw, err=e)}[/]")
            continue
        if parsed.msg_id is None:
            console.print(f"[red]{i18n_tf('wiz_msg_ref_no_msgid', raw=raw)}[/]")
            continue
        return raw


async def _pick_mark_read(*, default: bool):
    """Yes/No/Back. Returns True, False, BACK, or None (cancel).

    Yes is listed first since it's the wizard default: after analyzing
    unread messages the user has effectively seen them, so advancing
    Telegram's read marker matches intent.
    """
    choices = [
        questionary.Choice(i18n_t("wiz_mark_read_yes"), value=True),
        questionary.Choice(i18n_t("wiz_mark_read_no"), value=False),
        questionary.Separator(),
        questionary.Choice(i18n_t("wiz_back"), value=BACK),
    ]
    picked = await _bind_escape(
        questionary.select(
            i18n_t("wiz_mark_read_q"),
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
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    yn = i18n_t("wiz_summary_yes") if picked else i18n_t("wiz_summary_no")
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_mark_read')}: [bold]{yn}[/]")
    return picked


async def _pick_period(
    *,
    counts: dict[str, int | None] | None = None,
    static_only: bool = False,
):
    """Returns (period_key, since, until, from_msg), BACK, or None.

    `counts` is an optional per-period message-count hint; if given, each
    choice is annotated with the count so the user can see how much work
    they're about to buy. `from_msg` is populated only when the user picks
    "From message" — otherwise it's None.

    `static_only=True` hides "Custom date range…" and "From a specific
    message…". Used by `unread tg chats add` where the persisted `period`
    field has to be a static key (unread/last7/last30/full) — a
    one-shot date range or msg id can't be the recurring default for
    `unread tg chats run`.

    "Unread" is always available — including for all-flat forum mode, where
    it resolves to "since the forum's dialog-level read marker" (the same
    marker Telegram uses for the unread badge). If the user needs per-topic
    unread, --all-per-topic is the right mode instead.
    """
    c = counts or {}

    def _label(base: str, key: str) -> str:
        n = c.get(key)
        if n is None:
            return base
        return f"{base}  [{i18n_tf('wiz_period_n_msgs', n=n)}]"

    options: list[Any] = [
        questionary.Choice(title=_label(i18n_t("wiz_period_unread"), "unread"), value="unread"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last24h"), "last24h"), value="last24h"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last96h"), "last96h"), value="last96h"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last7"), "last7"), value="last7"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last30"), "last30"), value="last30"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last90"), "last90"), value="last90"),
        questionary.Choice(title=_label(i18n_t("wiz_period_year_start"), "year_start"), value="year_start"),
        questionary.Choice(title=_label(i18n_t("wiz_period_full"), "full"), value="full"),
    ]
    if not static_only:
        options.append(questionary.Choice(title=i18n_t("wiz_period_from_msg"), value="from_msg"))
        options.append(questionary.Choice(title=i18n_t("wiz_period_custom"), value="custom"))
    options.append(questionary.Separator())
    options.append(questionary.Choice(title=i18n_t("wiz_back"), value=BACK))
    key = await _bind_escape(
        questionary.select(
            i18n_t("wiz_pick_period"),
            choices=options,
            use_jk_keys=False,
            style=LIST_STYLE,
        ),
        BACK,
    ).ask_async()
    if key is None:
        return None
    if key is BACK:
        _replace_last_line(f"[grey70]{i18n_t('wiz_back')}[/]")
        return BACK
    _period_label_keys = {
        "unread": "wiz_summary_period_unread",
        "last24h": "wiz_summary_period_last24h",
        "last96h": "wiz_summary_period_last96h",
        "last7": "wiz_summary_period_last7",
        "last30": "wiz_summary_period_last30",
        "last90": "wiz_summary_period_last90",
        "year_start": "wiz_summary_period_year_start",
        "full": "wiz_summary_period_full",
        "custom": "wiz_summary_period_custom",
        "from_msg": "wiz_summary_period_from_msg",
    }
    label_key = _period_label_keys.get(key)
    label = i18n_t(label_key) if label_key else key
    n = c.get(key) if isinstance(key, str) else None
    label_with_count = i18n_tf("wiz_summary_period_with_count", label=label, n=n) if n is not None else label
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_period')}: [bold]{label_with_count}[/]")
    if key == "from_msg":
        ref = await _prompt_msg_ref()
        if ref is None:
            # Cancelled the sub-prompt → bounce back to the period picker
            # rather than the whole wizard. Gives the user a way out of
            # "I meant to pick last-7" without losing their chat / preset
            # choice so far.
            return await _pick_period(counts=counts)
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_period')}: "
            f"[bold]{i18n_tf('wiz_summary_step_period_from', ref=ref)}[/]"
        )
        return key, None, None, ref
    if key == "custom":
        since = await questionary.text(i18n_t("wiz_custom_since_prompt"), default="").ask_async()
        until = await questionary.text(i18n_t("wiz_custom_until_prompt"), default="").ask_async()
        if since is None or until is None:
            return None
        for val in (since, until):
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    console.print(f"[red]{i18n_tf('wiz_bad_date', value=val)}[/]")
                    return await _pick_period(counts=counts)
        return key, since or None, until or None, None
    return key, None, None, None


__all__ = [
    "ALL_LOCAL",
    "ALL_UNREAD",
    "BACK",
    "InteractiveAnswers",
    "Path",
    "build_analyze_args",
    "build_dump_args",
    "run_interactive_analyze",
    "run_interactive_ask",
    "run_interactive_describe",
    "run_interactive_dump",
]
