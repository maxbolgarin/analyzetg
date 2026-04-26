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

from analyzetg.analyzer.prompts import Preset, get_presets
from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.i18n import t as i18n_t
from analyzetg.i18n import tf as i18n_tf
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import list_unread_dialogs
from analyzetg.tg.topics import list_forum_topics
from analyzetg.util.logging import get_logger


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

# Sentinel returned by _pick_chat when the user picks "Search ALL
# synced chats (local DB)". Triggers the no-scope local query path —
# zero TG round-trips, retrieval reads every synced chat.
ALL_LOCAL = object()

# Rough token estimate per formatted message line (sender + timestamp + body).
# Used only for up-front cost previews; the real pipeline counts exactly via
# tiktoken. Cyrillic runs ~1.5x the English rate — this is a middle ground.
_AVG_TOKENS_PER_MSG = 60


def _bind_escape(question, value):
    """Make ESC exit the questionary prompt with `value`.

    Use `BACK` on steps that have a back action; use `None` on the first step
    (same semantics as Ctrl-C there). `eager=True` so we win over any default
    ESC behaviour (e.g. clearing the search filter)."""

    @question.application.key_bindings.add(Keys.Escape, eager=True)
    def _(event):
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
    # Period keys: "unread" | "last7" | "last30" | "full" | "custom" | "from_msg"
    period: str
    custom_since: str | None
    custom_until: str | None
    console_out: bool
    mark_read: bool
    output_path: Path | None = None
    run_on_all_unread: bool = False  # User picked "Run on ALL N unread chats"
    run_on_all_local: bool = False  # ask mode: "🌐 ALL synced chats" picked
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
    elif period == "last7":
        out["since"] = datetime.now(UTC) - timedelta(days=7)
    elif period == "last30":
        out["since"] = datetime.now(UTC) - timedelta(days=30)
    elif period == "custom":
        if custom_since:
            out["since"] = datetime.strptime(custom_since, "%Y-%m-%d").replace(tzinfo=UTC)
        if custom_until:
            out["until"] = datetime.strptime(custom_until, "%Y-%m-%d").replace(tzinfo=UTC)
    elif period == "from_msg" and custom_from_msg:
        from analyzetg.tg.links import parse as _parse_link

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
    full_history = False
    since: str | None = None
    until: str | None = None
    from_msg: str | None = None
    if answers.period == "last7":
        last_days = 7
    elif answers.period == "last30":
        last_days = 30
    elif answers.period == "full":
        full_history = True
    elif answers.period == "custom":
        since = answers.custom_since
        until = answers.custom_until
    elif answers.period == "from_msg" and include_from_msg:
        from_msg = answers.custom_from_msg
    out: dict[str, Any] = {
        "last_days": last_days,
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
    content_language: str | None = None,
) -> None:
    """Default UX for `analyzetg analyze` (no ref). Walk wizard, then run.

    CLI flags that have no wizard step (`--post-saved`, `--max-cost`,
    `--self-check`, `--cite-context`, `--no-cache`, `--dry-run`, `--by`,
    `--post-to`) are forwarded as-is so the wizard path matches the direct
    path when the user typed `atg analyze --self-check --post-saved`.
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
        from analyzetg.analyzer.commands import run_all_unread_analyze

        await run_all_unread_analyze(
            preset=answers.preset,
            output=answers.output_path,
            console_out=answers.console_out,
            mark_read=answers.mark_read,
            yes=True,  # wizard already confirmed the plan, no second prompt
            language=language,
            content_language=content_language,
        )
        return

    from analyzetg.analyzer.commands import cmd_analyze

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
    args["content_language"] = content_language
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
    content_language: str | None = None,
) -> None:
    """Default UX for `analyzetg dump` (no ref). Wizard without preset step."""
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
        from analyzetg.export.commands import run_all_unread_dump

        await run_all_unread_dump(
            fmt=fmt,
            output=answers.output_path,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=answers.console_out,
            mark_read=answers.mark_read,
            yes=True,
            language=language,
            content_language=content_language,
            **_build_enrich_kwargs(answers),
        )
        return

    from analyzetg.export.commands import cmd_dump

    args = build_dump_args(
        answers,
        fmt=fmt,
        with_transcribe=with_transcribe,
        include_transcripts=include_transcripts,
    )
    args["language"] = language
    args["content_language"] = content_language
    await cmd_dump(**args)


async def run_interactive_describe() -> None:
    """Default UX for `analyzetg describe` (no ref, no filters): pick → show."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        console.print(f"[bold cyan]{i18n_t('wiz_pick_chat_to_describe')}[/]")
        console.print(f"[dim]{i18n_t('wiz_tips')}[/]\n")
        chat = await _pick_chat(client, offer_all_unread=False)
        if chat is None or chat is ALL_UNREAD:
            console.print(f"[dim]{i18n_t('cancelled')}[/]")
            return
        chat_ref = str(chat["chat_id"])

    # Now open a fresh session via the existing cmd_describe flow.
    from analyzetg.tg.commands import cmd_describe

    await cmd_describe(chat_ref)


def _period_to_cli_kwargs(answers: InteractiveAnswers) -> dict[str, Any]:
    """Map the wizard's period choice to cmd_ask's CLI kwargs.

    cmd_ask doesn't expose --full-history (use --global instead) or
    --from-msg, so those wizard choices collapse to "no period filter".
    """
    p = answers.period
    if p == "last7":
        return {"last_days": 7}
    if p == "last30":
        return {"last_days": 30}
    if p == "custom":
        return {"since": answers.custom_since, "until": answers.custom_until}
    if p == "from_msg":
        return {}  # ask doesn't honour from_msg
    if p == "full":
        return {}
    return {}  # "unread" or anything else


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
    content_language: str | None = None,
) -> None:
    """Default UX for `atg ask` (no <ref>, no --chat/--folder/--global).

    Walks the ask-mode wizard, then dispatches to `cmd_ask` with the
    picked scope. The wizard never builds the embeddings index or
    invokes --build-index — that's an explicit user decision.
    """
    answers = await _collect_answers(
        mode="ask",
        console_out=console_out,
        output=output,
        save_default=False,
        mark_read=None,
        question=question,
    )
    if answers is None:
        return

    from analyzetg.ask.commands import cmd_ask

    period_kwargs = _period_to_cli_kwargs(answers)

    chat_arg: str | None = None
    if answers.chat_ref:  # non-empty ref → use it
        chat_arg = answers.chat_ref

    # TODO(task-4): add ref/global_scope/no_followup parameters on cmd_ask;
    # until then, `run_interactive_ask` is unreachable from the live CLI.
    await cmd_ask(
        question=question,
        ref=None,
        chat=chat_arg,
        thread=answers.thread_id,
        folder=None,
        global_scope=answers.run_on_all_local,
        refresh=refresh,
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
        content_language=content_language,
        build_index=False,
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
      - "ask" skips preset / enrich / output / mark_read; runs chat → [comments|thread] → period → confirm.
        Ask mode also offers an "ALL synced chats (local DB)" row in the
        chat picker; picking it sets `run_on_all_local=True` and jumps
        straight to period (no thread / comments).

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
        # only "atg dump" in the scrollback, it's easy to forget which command
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
            console.print(f"  [dim]output (from CLI):[/]    [bold]{out_label}[/]")
        if mark_read_forced:
            console.print(f"  [dim]mark read (from CLI):[/] [bold]{'yes' if mark_read else 'no'}[/]")
        console.print(f"[dim]{i18n_t('wiz_tips')}[/]\n")

        chat: dict | None = None
        thread_id: int | None = None
        forum_all_flat = False
        forum_all_per_topic = False
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
        # Local, step-level state for output + mark_read — start from CLI
        # overrides when present, otherwise get set by the wizard steps.
        chosen_console_out = bool(console_out)
        chosen_output_path: Path | None = output
        # Default mark-read to True in the wizard: if the user ran analyze
        # on unread messages they've effectively "seen" them now, so
        # advancing Telegram's read marker matches intent. CLI
        # `--no-mark-read` can still override.
        chosen_mark_read: bool = bool(mark_read) if mark_read is not None else True
        # Per-period message counts for the current chat (filled once we
        # know the chat and, for forums, the thread). Used by `_pick_period`
        # to decorate choices and by the confirm step to estimate cost.
        period_counts: dict[str, int | None] = {}

        run_on_all = False
        run_on_all_local = False
        step = "chat"
        while True:
            if step == "chat":
                # Ask mode swaps "Run on all N unread" for "Search ALL
                # synced chats (local DB)" — the analyze/dump batch flow
                # doesn't make sense for ask (one question across many
                # chats is the global ALL_LOCAL path).
                # NOTE: when the user has zero unread dialogs, _pick_chat
                # early-returns to _pick_from_all BEFORE the offer_all_local
                # block runs, so the ALL_LOCAL row is hidden in exactly the
                # case where ask-mode users would want it most. Tracked for
                # a follow-up task; not fixed here.
                result = await _pick_chat(
                    client,
                    offer_all_unread=(mode != "ask"),
                    offer_all_local=(mode == "ask"),
                )
                if result is None:
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
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
                        from analyzetg.tg.topics import get_linked_chat_id

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
                elif mode == "ask":
                    # Ask mode skips preset/enrich/output/mark_read.
                    step = "period"
                else:
                    # Dump: skip preset (no preset for dump), go straight
                    # to enrich since media enrichment applies here too.
                    step = "enrich"

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
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                with_comments = bool(result)
                step = "preset" if mode == "analyze" else "period" if mode == "ask" else "enrich"

            elif step == "thread":
                result = await _pick_thread(client, chat["chat_id"])
                if result is BACK:
                    step = "chat"
                    continue
                if result is None:
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                thread_id, forum_all_flat, forum_all_per_topic = result
                step = "preset" if mode == "analyze" else "period" if mode == "ask" else "enrich"

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
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                preset = result
                step = _next_step_after_mark_read(output_forced, mark_read_forced) if run_on_all else "period"

            elif step == "period":
                # Lazily fetch per-period counts once we know chat+thread.
                # `unread_hint` comes from the dialog picker (chat object).
                # ALL_LOCAL (ask mode) has no chat scope, so skip the
                # per-chat count fetch — counts are simply absent.
                if not period_counts and chat is not None:
                    unread_hint = int(chat.get("unread") or 0)
                    period_counts = await _fetch_period_counts(
                        client,
                        chat_id=int(chat["chat_id"]),
                        thread_id=thread_id,
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
                    elif mode == "ask" and chat and chat["kind"] == "channel" and linked_chat_id is not None:
                        step = "comments"
                    else:
                        # Includes ask + ALL_LOCAL (run_on_all_local) and
                        # ask + private/group chats: back to chat picker.
                        step = "chat"
                    continue
                if result is None:
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                period, custom_since, custom_until, custom_from_msg = result
                # For a custom date range, estimate the message count on
                # demand so the confirm step can show "messages ≈ N" and
                # the cost estimate (analyze mode) has a number to work
                # with. Cheap: 2 get_messages(limit=1) calls.
                if period == "custom" and chat is not None and (custom_since or custom_until):
                    period_counts["custom"] = await _count_custom_range(
                        client,
                        chat_id=int(chat["chat_id"]),
                        thread_id=thread_id,
                        since=datetime.strptime(custom_since, "%Y-%m-%d") if custom_since else None,
                        until=datetime.strptime(custom_until, "%Y-%m-%d") if custom_until else None,
                    )
                # Ask mode skips enrich/output/mark_read entirely.
                step = "confirm" if mode == "ask" else "enrich"

            elif step == "enrich":
                # Runs for both analyze and dump so media-to-text
                # conversion flows into either output path. Counts are
                # period-scoped: filter the local DB by the time/msg-id
                # window the user just picked so "(N in db)" reflects
                # what the run will actually process.
                media_counts: dict[str, int] = {}
                if chat is not None:
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
                        f"[dim]"
                        f"{i18n_tf('wiz_plan_for_period_synced', total=media_counts['total'], extras=extras)}"
                        f"[/]"
                    )
                result = await _pick_enrich(media_counts=media_counts)
                if result is BACK:
                    step = "period"
                    continue
                if result is None:
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                enrich_kinds = list(result) if isinstance(result, list) else None
                # Output step is next unless the user already set it via CLI.
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
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                chosen_console_out, chosen_output_path = result
                step = "confirm" if mark_read_forced else "mark_read"

            elif step == "mark_read":
                result = await _pick_mark_read(default=chosen_mark_read)
                if result is BACK:
                    if output_forced:
                        # No output step → go back to enrich (or preset/chat
                        # for run-on-all where period+enrich are skipped).
                        step = "enrich" if not run_on_all else ("preset" if mode == "analyze" else "chat")
                    else:
                        step = "output"
                    continue
                if result is None:
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                chosen_mark_read = bool(result)
                step = "confirm"

            elif step == "confirm":
                summary_bits = []
                if run_on_all:
                    summary_bits.append(i18n_t("wiz_plan_all_unread_chats"))
                elif run_on_all_local:
                    # Ask mode "search ALL synced chats" path. Re-uses the
                    # picker label for consistency with the chat-step echo.
                    summary_bits.append(i18n_t("wiz_ask_all_local"))
                else:
                    summary_bits.append(chat.get("title") or str(chat["chat_id"]))
                if thread_id:
                    summary_bits.append(i18n_tf("wiz_plan_topic", id=thread_id))
                if forum_all_flat:
                    summary_bits.append(i18n_t("wiz_plan_all_flat"))
                if forum_all_per_topic:
                    summary_bits.append(i18n_t("wiz_plan_per_topic"))
                if mode == "analyze":
                    summary_bits.append(i18n_tf("wiz_plan_preset_kv", preset=preset))
                    # Show the enrichment choice explicitly so the user can
                    # sanity-check it before spending — the step happens early
                    # in the wizard and is easy to forget by the time we hit
                    # confirm.
                    if enrich_kinds is not None:
                        if enrich_kinds:
                            summary_bits.append(i18n_tf("wiz_plan_enrich_kv", kinds=",".join(enrich_kinds)))
                        else:
                            summary_bits.append(i18n_t("wiz_plan_enrich_none"))
                if not run_on_all:
                    summary_bits.append(i18n_tf("wiz_plan_period_kv", period=period))
                    if period == "custom":
                        # Include the estimated count next to the date
                        # range so the user sees the scope at confirm time.
                        n = period_counts.get("custom") if period_counts else None
                        range_str = f"{custom_since or ''}..{custom_until or ''}"
                        summary_bits.append(
                            i18n_tf("wiz_plan_range_with_count", range=range_str, n=n)
                            if n is not None
                            else i18n_tf("wiz_plan_range", range=range_str)
                        )
                    elif period == "from_msg" and custom_from_msg:
                        summary_bits.append(i18n_tf("wiz_plan_from", ref=custom_from_msg))
                # Ask mode skips output / mark_read summary lines entirely.
                if mode != "ask":
                    summary_bits.append(
                        i18n_t("wiz_plan_console")
                        if chosen_console_out
                        else (
                            i18n_tf("wiz_plan_file_kv", path=chosen_output_path)
                            if chosen_output_path
                            else i18n_t("wiz_plan_save_reports")
                        )
                    )
                    if chosen_mark_read:
                        summary_bits.append(i18n_t("wiz_plan_mark_read"))
                # Question is supplied by the ask CLI on the side; show it
                # here so the user sees what they're about to ask.
                if mode == "ask" and question:
                    summary_bits.append(question)
                # Lead with the action (analyze / dump / …) so the confirm
                # line is unambiguous on its own — the user shouldn't have to
                # scroll up to remember which command they invoked.
                console.print(
                    f"[bold]{i18n_t('wiz_plan_label')} ([yellow]{mode}[/]):[/] " + " / ".join(summary_bits)
                )

                # Only show a cost estimate for the analyze flow (dump
                # doesn't hit OpenAI for chat completion) and when we have
                # a concrete count.
                if mode == "analyze" and not run_on_all and preset is not None:
                    n_msgs = _count_for_period(period, period_counts)
                    if n_msgs is not None and n_msgs > 0:
                        wizard_presets = get_presets(settings.locale.language or "en")
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
                        if cost_lo is None:
                            console.print(
                                f"  [dim]{i18n_t('wiz_plan_msgs_approx')}[/] {n_msgs}  "
                                f"[dim]{i18n_t('wiz_plan_pricing_missing')}[/]"
                            )
                        else:
                            console.print(
                                f"  [dim]{i18n_t('wiz_plan_msgs_approx')}[/] {n_msgs}  "
                                f"[dim]{i18n_t('wiz_plan_cost_approx')}[/] "
                                f"{_fmt_cost_range(cost_lo, cost_hi)}  "
                                f"[dim]{i18n_t('wiz_plan_analysis_estimate')}[/]"
                            )
                        # The analysis estimate doesn't include enrichment
                        # costs — we don't know per-message media counts at
                        # wizard time. Call it out so the user doesn't get
                        # surprised when `atg stats` shows extra spend.
                        extra_kinds = _extra_enrich_kinds(enrich_kinds)
                        if extra_kinds:
                            console.print(
                                f"  [dim yellow]{i18n_t('wiz_plan_extra_enrich_label')}[/] "
                                f"[yellow]{', '.join(extra_kinds)}[/] "
                                f"[dim]{i18n_t('wiz_plan_extra_enrich_hint')}[/] "
                                "[cyan]atg stats[/]"
                                f"[dim]{i18n_t('wiz_plan_extra_enrich_hint_close')}[/]"
                            )
                    elif n_msgs == 0:
                        console.print(f"  [yellow]{i18n_t('wiz_plan_zero_msgs')}[/]")
                elif mode == "dump" and not run_on_all:
                    n_msgs = _count_for_period(period, period_counts)
                    if n_msgs is not None:
                        console.print(
                            f"  [dim]{i18n_t('wiz_plan_msgs_approx')}[/] {n_msgs}  "
                            f"[dim]{i18n_t('wiz_plan_dump_free')}[/]"
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
                    console.print(f"[dim]{i18n_t('cancelled')}[/]")
                    return None
                if choice is BACK:
                    if mode == "ask":
                        # Ask mode confirm always backs to period (no
                        # output/mark_read steps were ever shown).
                        step = "period"
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
            run_on_all_unread=run_on_all,
            run_on_all_local=run_on_all_local,
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
    last7_start_task = _first_id_after(now - timedelta(days=7))
    last30_start_task = _first_id_after(now - timedelta(days=30))
    full_start_task = _first_id_after(None)

    latest, last7_start, last30_start, full_start = await _asyncio.gather(
        latest_task, last7_start_task, last30_start_task, full_start_task
    )

    def _count(start: int | None) -> int | None:
        if latest is None or start is None:
            return None
        return max(0, latest - start + 1)

    out: dict[str, int | None] = {
        "last7": _count(last7_start),
        "last30": _count(last30_start),
        "full": _count(full_start),
        # For "unread" we already have a hint from the dialog row.
        "unread": unread_hint if unread_hint else None,
    }
    # Sanity clamps. Periods can't exceed full; unread can't exceed the
    # period it falls inside (best-effort — unread_hint is server-authoritative
    # and trumps our approximation, so we only clamp the other direction).
    if out.get("full") is not None:
        for key in ("last7", "last30"):
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
    from analyzetg.analyzer.pipeline import estimate_cost as _est

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


def _fmt_cost_range(lo: float | None, hi: float | None) -> str:
    """Render a (lo, hi) cost range; collapse to one number if they're close."""
    if lo is None and hi is None:
        return "—"
    if lo is None or hi is None or abs((hi or 0) - (lo or 0)) < 1e-4:
        return _fmt_cost(lo if lo is not None else hi)
    return f"{_fmt_cost(lo)}–{_fmt_cost(hi)}"


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
    `atg chats add` see at a glance which dialogs they've already
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
    `atg chats list` without leaving the picker. Empty set / None
    behaves like before.

    `offer_all_local` (used by the ask wizard) adds a "Search ALL synced
    chats (local DB)" row that returns the `ALL_LOCAL` sentinel —
    triggers the no-scope local-only path (zero TG round-trips).

    Returns one of:
      - dict (picked chat) — a resolved entry
      - ALL_UNREAD — user picked "Run on all N unread chats" (if offer_all_unread)
      - ALL_LOCAL — user picked "Search ALL synced chats" (if offer_all_local)
      - None — cancelled
    """
    from analyzetg.tg.folders import chat_folder_index

    unread = await list_unread_dialogs(client)
    sub_set = subscribed_ids or set()

    if not unread:
        console.print(f"[yellow]{i18n_t('wiz_no_unread_showing_all')}[/]")
        return await _pick_from_all(client, subscribed_ids=sub_set)

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
    choices.append(questionary.Separator())
    choices.append(questionary.Separator(_chat_header_row()))
    for d in unread:
        choices.append(
            questionary.Choice(
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
        )
    # No "Back" on the first step — there's nowhere to go back to.
    # Ctrl-C cancels the whole wizard.

    result = await _bind_escape(
        questionary.select(
            i18n_tf("wiz_pick_chat_n_unread", n=len(unread)),
            choices=choices,
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
    if action == "all":
        _replace_last_line(
            f"[bold cyan]?[/] {i18n_t('wiz_summary_step_chat')}: "
            f"[dim]{i18n_t('wiz_summary_chat_searching_all')}[/]"
        )
        return await _pick_from_all(client, subscribed_ids=sub_set)
    d = payload
    _replace_last_line(
        f"[bold cyan]?[/] chat: [bold]{d.title or d.chat_id}[/] [dim]({d.kind}, {d.unread_count} unread)[/]"
    )
    return {
        "chat_id": d.chat_id,
        "kind": d.kind,
        "title": d.title,
        "username": d.username,
        "read_inbox_max_id": d.read_inbox_max_id,
        "unread": d.unread_count,
    }


async def _pick_from_all(client, *, subscribed_ids: set[int] | None = None) -> dict | None:
    """Scan every dialog and present a searchable list.

    `subscribed_ids` prefixes each subscribed row with a `★` so the
    `atg chats add` flow shows what's already on the subscription list.
    """
    from analyzetg.tg.client import _chat_kind, entity_id, entity_title, entity_username
    from analyzetg.tg.dialogs import UnreadDialog, correct_forum_unread

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
    from analyzetg.tg.folders import chat_folder_index

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
    choices: list[Any] = [questionary.Separator(header_line)]
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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
        return BACK
    mode_label = i18n_t("wiz_summary_step_mode")
    if action == "per_topic":
        _replace_last_line(
            f"[bold cyan]?[/] {mode_label}: [bold]{i18n_t('wiz_summary_mode_per_topic')}[/] "
            f"[dim]{i18n_t('wiz_summary_mode_per_topic_hint')}[/]"
        )
        return None, False, True
    if action == "flat":
        _replace_last_line(
            f"[bold cyan]?[/] {mode_label}: [bold]{i18n_t('wiz_summary_mode_all_flat')}[/] "
            f"[dim]{i18n_t('wiz_summary_mode_all_flat_hint')}[/]"
        )
        return None, True, False
    picked_topic = next((t for t in topics_sorted if t.topic_id == payload), None)
    label = picked_topic.title if picked_topic else str(payload)
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_topic')}: [bold]{label}[/]")
    return payload, False, False


async def _pick_preset():
    """Returns preset name (str), BACK, or None.

    Reads presets from `presets/<active-language>/`. Each preset's
    `description` frontmatter field is shown next to its name; new
    presets are picked up automatically (drop a `.md` into the language
    directory with `description:` set).
    """
    from analyzetg.config import get_settings

    active_lang = (get_settings().locale.language or "en").lower()
    presets_for_lang = get_presets(active_lang)

    preferred = [
        "summary",
        "broad",
        "digest",
        "highlights",
        "action_items",
        "decisions",
        "questions",
        "quotes",
        "links",
        "reactions",
        "single_msg",
        "multichat",
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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
        return BACK
    _replace_last_line(f"[bold cyan]?[/] {i18n_t('wiz_summary_step_preset')}: [bold]{picked}[/]")
    return picked


async def _pick_output(*, default_path: Path | None):
    """Returns (console_out, output_path), BACK, or None (cancel).

    `default_path` seeds the custom-path prompt so the user can edit an
    already-provided value instead of retyping it.
    """
    choices = [
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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
        return BACK
    out_label = i18n_t("wiz_summary_step_output")
    if action == "console":
        _replace_last_line(f"[bold cyan]?[/] {out_label}: [bold]{i18n_t('wiz_summary_step_console')}[/]")
        return True, None
    if action == "file":
        _replace_last_line(
            f"[bold cyan]?[/] {out_label}: [bold]{i18n_t('wiz_summary_step_reports_dir')}[/] "
            f"[dim]{i18n_t('wiz_summary_step_auto_named')}[/]"
        )
        return False, None
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
    return False, path


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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
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
    from analyzetg.tg.links import parse as _parse_link

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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
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
    message…". Used by `atg chats add` where the persisted `period`
    field has to be a static key (unread/last7/last30/full) — a
    one-shot date range or msg id can't be the recurring default for
    `atg chats run`.

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
        questionary.Choice(title=_label(i18n_t("wiz_period_last7"), "last7"), value="last7"),
        questionary.Choice(title=_label(i18n_t("wiz_period_last30"), "last30"), value="last30"),
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
        _replace_last_line(f"[dim]{i18n_t('wiz_back')}[/]")
        return BACK
    _period_label_keys = {
        "unread": "wiz_summary_period_unread",
        "last7": "wiz_summary_period_last7",
        "last30": "wiz_summary_period_last30",
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
