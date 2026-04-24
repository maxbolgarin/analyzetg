"""Interactive wizard: pick chat → thread → preset → period → run analyze.

The I/O side (`run_interactive`) uses `questionary` for arrow-key menus
with type-to-filter. The pure arg-builder (`build_analyze_args`) turns a
structured answer dict into `cmd_analyze` kwargs — unit-testable without
a Telegram client.
"""

from __future__ import annotations

import asyncio as _asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import questionary
from prompt_toolkit.keys import Keys
from rich.console import Console

from analyzetg.analyzer.chunker import model_context_window
from analyzetg.analyzer.prompts import PRESETS, Preset
from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import list_unread_dialogs
from analyzetg.tg.topics import list_forum_topics
from analyzetg.util.logging import get_logger
from analyzetg.util.pricing import chat_cost

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
    def _select(event):
        choice = ic.get_pointed_at()
        if choice is None:
            return
        if choice.value not in ic.selected_options:
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
    preset: str
    # Period keys: "unread" | "last7" | "last30" | "full" | "custom" | "from_msg"
    period: str
    custom_since: str | None
    custom_until: str | None
    console_out: bool
    mark_read: bool
    output_path: Path | None = None
    run_on_all_unread: bool = False  # User picked "Run on ALL N unread chats"
    # None = "use defaults" (config.toml + preset); [] = "disable everything";
    # non-empty list = "enable exactly these kinds" (unioned with preset.enrich_kinds
    # by cmd_analyze via --enrich=<csv>).
    enrich_kinds: list[str] | None = None
    # Set only when period == "from_msg": a Telegram message link OR bare
    # msg_id string. Passed through to cmd_analyze's --from-msg unchanged
    # (cmd_analyze does the link parsing).
    custom_from_msg: str | None = None


def build_analyze_args(answers: InteractiveAnswers) -> dict[str, Any]:
    """Turn interactive answers into `cmd_analyze` kwargs. Pure."""
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
    elif answers.period == "from_msg":
        from_msg = answers.custom_from_msg

    # Enrichment flags: None (wizard was skipped / defaults) vs empty list
    # (user explicitly disabled all) vs populated list (explicit set).
    enrich_csv: str | None = None
    no_enrich = False
    if answers.enrich_kinds is not None:
        if not answers.enrich_kinds:
            no_enrich = True
        else:
            enrich_csv = ",".join(answers.enrich_kinds)

    return {
        "ref": answers.chat_ref,
        "thread": answers.thread_id,
        "from_msg": from_msg,
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
        "save_default": False,
        "mark_read": answers.mark_read,
        "no_cache": False,
        "include_transcripts": True,
        "min_msg_chars": None,
        "enrich": enrich_csv,
        "enrich_all": False,
        "no_enrich": no_enrich,
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

    # Enrichment: same tri-state as build_analyze_args. None (wizard
    # skipped) → config defaults; [] → explicit "off"; populated list
    # → explicit "on set".
    enrich_csv: str | None = None
    no_enrich = False
    if answers.enrich_kinds is not None:
        if not answers.enrich_kinds:
            no_enrich = True
        else:
            enrich_csv = ",".join(answers.enrich_kinds)

    return {
        "ref": answers.chat_ref,
        "output": answers.output_path,
        "fmt": fmt,
        "since": since,
        "until": until,
        "last_days": last_days,
        "full_history": full_history,
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
        "enrich": enrich_csv,
        "enrich_all": False,
        "no_enrich": no_enrich,
    }


async def run_interactive_analyze(
    *,
    console_out: bool = False,
    output: Path | None = None,
    save_default: bool = False,
    mark_read: bool | None = None,
) -> None:
    """Default UX for `analyzetg analyze` (no ref). Walk wizard, then run."""
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
        )
        return

    from analyzetg.analyzer.commands import cmd_analyze

    await cmd_analyze(**build_analyze_args(answers))


async def run_interactive_dump(
    *,
    fmt: str = "md",
    output: Path | None = None,
    save_default: bool = False,
    with_transcribe: bool = False,
    include_transcripts: bool = True,
    console_out: bool = False,
    mark_read: bool | None = None,
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
        )
        return

    from analyzetg.export.commands import cmd_dump

    await cmd_dump(
        **build_dump_args(
            answers,
            fmt=fmt,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
        )
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
    save_default: bool,
    mark_read: bool | None,
) -> InteractiveAnswers | None:
    """State-machine wizard: each step can go back one without losing context.

    `mode` controls which steps appear: "analyze" walks through preset;
    "dump" skips the preset step. CLI flags pre-fill steps and skip the
    corresponding prompt:
      - `console_out`, `output`, or `save_default` → skip the output step.
      - `mark_read is not None` → skip the mark-read step.
    """
    settings = get_settings()
    # Whether the user already made these choices at the CLI. If so, the
    # matching wizard step is suppressed and the forced value is used.
    output_forced = console_out or output is not None or save_default
    mark_read_forced = mark_read is not None

    async with tg_client(settings) as client, open_repo(settings.storage.data_path):
        console.print("[bold cyan]analyzetg[/] — interactive mode")
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
        console.print(
            "[dim]Tips:[/] "
            "[bold]type letters to filter[/] ([dim]e.g. [cyan]uni[/] → UNION[/]), "
            "[dim]↑/↓ navigate, Enter select, ESC back, Ctrl-C cancel.[/]\n"
        )

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
                    step = (
                        "preset"
                        if mode == "analyze"
                        else _next_step_after_mark_read(output_forced, mark_read_forced)
                    )
                    continue
                chat = result
                if chat["kind"] == "forum":
                    step = "thread"
                elif mode == "analyze":
                    step = "preset"
                else:
                    # Dump: skip preset (no preset for dump), go straight
                    # to enrich since media enrichment applies here too.
                    step = "enrich"

            elif step == "thread":
                result = await _pick_thread(client, chat["chat_id"])
                if result is BACK:
                    step = "chat"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                thread_id, forum_all_flat, forum_all_per_topic = result
                step = "preset" if mode == "analyze" else "enrich"

            elif step == "preset":
                # Only runs for analyze mode.
                result = await _pick_preset()
                if result is BACK:
                    step = (
                        "chat" if run_on_all else ("thread" if chat and chat["kind"] == "forum" else "chat")
                    )
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                preset = result
                step = _next_step_after_mark_read(output_forced, mark_read_forced) if run_on_all else "enrich"

            elif step == "enrich":
                # Runs for both analyze and dump so media-to-text
                # conversion flows into either output path.
                result = await _pick_enrich()
                if result is BACK:
                    # Go back to the step that preceded us: preset for
                    # analyze mode, thread (forum) or chat (non-forum)
                    # for dump mode.
                    if mode == "analyze":
                        step = "preset"
                    elif chat and chat["kind"] == "forum":
                        step = "thread"
                    else:
                        step = "chat"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                enrich_kinds = list(result) if isinstance(result, list) else None
                step = "period"

            elif step == "period":
                # Lazily fetch per-period counts once we know chat+thread.
                # `unread_hint` comes from the dialog picker (chat object).
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
                    # Both modes run through `enrich` → `period`, so
                    # Back from period lands on enrich regardless.
                    step = "enrich"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
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
                # Output step is next unless the user already set it via CLI.
                step = "mark_read" if output_forced else "output"

            elif step == "output":
                result = await _pick_output(
                    default_path=output,
                )
                if result is BACK:
                    step = "period" if not run_on_all else ("preset" if mode == "analyze" else "chat")
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                chosen_console_out, chosen_output_path = result
                step = "confirm" if mark_read_forced else "mark_read"

            elif step == "mark_read":
                result = await _pick_mark_read(default=chosen_mark_read)
                if result is BACK:
                    if output_forced:
                        # No output step → go back to period / preset.
                        step = "period" if not run_on_all else ("preset" if mode == "analyze" else "chat")
                    else:
                        step = "output"
                    continue
                if result is None:
                    console.print("[dim]Cancelled.[/]")
                    return None
                chosen_mark_read = bool(result)
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
                    # Show the enrichment choice explicitly so the user can
                    # sanity-check it before spending — the step happens early
                    # in the wizard and is easy to forget by the time we hit
                    # confirm.
                    if enrich_kinds is not None:
                        if enrich_kinds:
                            summary_bits.append(f"enrich={','.join(enrich_kinds)}")
                        else:
                            summary_bits.append("enrich=none")
                if not run_on_all:
                    summary_bits.append(f"period={period}")
                    if period == "custom":
                        # Include the estimated count next to the date
                        # range so the user sees the scope at confirm time.
                        n = period_counts.get("custom") if period_counts else None
                        range_str = f"{custom_since or ''}..{custom_until or ''}"
                        summary_bits.append(f"({range_str}, {n} msgs)" if n is not None else f"({range_str})")
                    elif period == "from_msg" and custom_from_msg:
                        summary_bits.append(f"(from {custom_from_msg})")
                summary_bits.append(
                    "console"
                    if chosen_console_out
                    else (f"file={chosen_output_path}" if chosen_output_path else "save to reports/")
                )
                if chosen_mark_read:
                    summary_bits.append("mark-read")
                console.print("[bold]Plan:[/] " + " / ".join(summary_bits))

                # Only show a cost estimate for the analyze flow (dump
                # doesn't hit OpenAI for chat completion) and when we have
                # a concrete count.
                if mode == "analyze" and not run_on_all and preset is not None:
                    n_msgs = _count_for_period(period, period_counts)
                    if n_msgs is not None and n_msgs > 0:
                        cost_lo, cost_hi = _estimate_cost(
                            n_messages=n_msgs,
                            preset=PRESETS.get(preset) or PRESETS["summary"],
                            settings=settings,
                        )
                        if cost_lo is None:
                            console.print(
                                f"  [dim]messages ≈[/] {n_msgs}  "
                                "[dim](pricing table missing a model — cost unknown)[/]"
                            )
                        else:
                            console.print(
                                f"  [dim]messages ≈[/] {n_msgs}  "
                                f"[dim]cost ≈[/] {_fmt_cost_range(cost_lo, cost_hi)}  "
                                "[dim](analysis only; rough estimate)[/]"
                            )
                        # The analysis estimate doesn't include enrichment
                        # costs — we don't know per-message media counts at
                        # wizard time. Call it out so the user doesn't get
                        # surprised when `atg stats` shows extra spend.
                        extra_kinds = _extra_enrich_kinds(enrich_kinds)
                        if extra_kinds:
                            console.print(
                                "  [dim yellow]+ enrichment on:[/] "
                                f"[yellow]{', '.join(extra_kinds)}[/] "
                                "[dim](adds ~$0.003/min of audio, "
                                "~$0.0002/photo, ~$0.0001/link; actual cost per run visible in [/]"
                                "[cyan]atg stats[/][dim])[/]"
                            )
                    elif n_msgs == 0:
                        console.print("  [yellow]0 messages in this period — nothing to analyze.[/]")
                elif mode == "dump" and not run_on_all:
                    n_msgs = _count_for_period(period, period_counts)
                    if n_msgs is not None:
                        console.print(f"  [dim]messages ≈[/] {n_msgs}  [dim](dump is free — no OpenAI).[/]")

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
                    if mark_read_forced:
                        step = (
                            "output"
                            if not output_forced
                            else ("period" if not run_on_all else ("preset" if mode == "analyze" else "chat"))
                        )
                    else:
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
            console_out=chosen_console_out,
            mark_read=chosen_mark_read,
            output_path=chosen_output_path,
            run_on_all_unread=run_on_all,
            enrich_kinds=enrich_kinds,
            custom_from_msg=custom_from_msg,
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
    """Return (lower, upper) cost estimate in USD for the map-reduce pipeline.

    Approximations:
      - ~60 tokens / formatted message (Cyrillic-heavy middle ground).
      - Chunk budget mirrors `chunker.build_chunks`: context − system/user
        overhead − per-chunk output cap − safety margin.
      - Every chunk re-sends the system prompt (pipeline's actual behaviour).
      - Reduce input = Σ map-output tokens; reduce output ≤ output_budget_tokens.

    Returns (None, None) if pricing for either model is missing.
    """
    from analyzetg.util.tokens import count_tokens as _ct

    total_input_body = max(1, int(n_messages * _AVG_TOKENS_PER_MSG))

    filter_model = preset.filter_model
    final_model = preset.final_model
    filter_row = settings.pricing.chat.get(filter_model)
    final_row = settings.pricing.chat.get(final_model)
    if filter_row is None or final_row is None:
        return None, None

    system_tokens = _ct(preset.system, filter_model)
    user_overhead_tokens = _ct(preset.user_template, filter_model)
    per_chunk_overhead = system_tokens + user_overhead_tokens

    context = model_context_window(filter_model)
    safety = int(getattr(settings.analyze, "safety_margin_tokens", 4000))
    map_out_cap = preset.map_output_tokens
    budget = max(500, context - per_chunk_overhead - map_out_cap - safety)

    chunks = max(1, math.ceil(total_input_body / budget))

    # Map phase (filter model): every chunk re-sends system + user overhead.
    map_input_tokens = total_input_body + chunks * per_chunk_overhead
    # Map completion: cap per chunk; lower bound ≈ 40% of cap.
    map_out_lo = int(chunks * map_out_cap * 0.4)
    map_out_hi = int(chunks * map_out_cap)

    # Reduce phase (final model) — only if we built more than one chunk.
    if chunks > 1 and preset.needs_reduce:
        # Reduce prompt = aggregated map outputs + small final-prompt overhead.
        reduce_overhead = _ct(preset.system, final_model) + _ct(preset.user_template, final_model)
        reduce_out = preset.output_budget_tokens
        reduce_input_lo = map_out_lo + reduce_overhead
        reduce_input_hi = map_out_hi + reduce_overhead
    else:
        reduce_input_lo = reduce_input_hi = 0
        reduce_out = 0

    def _cost(prompt: int, completion: int, model: str) -> float:
        return float(chat_cost(model, prompt, 0, completion, settings=settings) or 0.0)

    lo = _cost(map_input_tokens, map_out_lo, filter_model) + _cost(
        reduce_input_lo, int(reduce_out * 0.4), final_model
    )
    hi = _cost(map_input_tokens, map_out_hi, filter_model) + _cost(reduce_input_hi, reduce_out, final_model)
    return lo, hi


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


def _chat_row(
    *,
    unread: int,
    kind: str,
    last_msg_date: datetime | None,
    title: str | int | None,
) -> str:
    """One formatted row in the chat-picker table.

    Two spaces between columns (not `·`) — the aligned whitespace reads
    as columns on its own, and dots just add visual noise at typical
    terminal widths. Title is unpadded (trails everything).
    """
    return (
        f"{_fmt_count(unread)}  "
        f"{_short_kind(kind):<{_COL_KIND}}  "
        f"{_fmt_date(last_msg_date):<{_COL_DATE}}  "
        f"{title or ''}"
    )


def _chat_header_row() -> str:
    return f"{'unread':>{_COL_UNREAD}}  {'kind':<{_COL_KIND}}  {'last msg':<{_COL_DATE}}  title"


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

    # Column header as a non-selectable separator at the top.
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
    choices.append(questionary.Separator(_chat_header_row()))
    for d in unread:
        choices.append(
            questionary.Choice(
                title=_chat_row(
                    unread=d.unread_count,
                    kind=d.kind,
                    last_msg_date=d.last_msg_date,
                    title=d.title or d.chat_id,
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
        "unread": d.unread_count,
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

    # Mirror the shape of _pick_chat's table so navigation between the two
    # pickers isn't visually jarring. Last-msg-date isn't available from
    # iter_dialogs here without extra fetches — leave it blank.
    header_line = f"{'unread':>{_COL_UNREAD}}  {'kind':<{_COL_KIND}}  title"
    choices: list[Any] = [questionary.Separator(header_line)]
    choices.extend(
        questionary.Choice(
            title=(
                f"{_fmt_count(r['unread'])}  "
                f"{_short_kind(r['kind']):<{_COL_KIND}}  "
                f"{r['title'] or r['chat_id']}"
            ),
            value=r,
        )
        for r in rows
    )

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
            f"{len(topics)} topic(s) in this forum (type to filter)",
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
        "broad",
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
        "summary": "Главное + идеи/решения + что посмотреть — концентрат без пересказа (дефолт)",
        "broad": "Полный обзор: Топ-3 темы + тезисы + настроение + ключевые сообщения",
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
            "Pick a preset (type to filter)",
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


async def _pick_output(*, default_path: Path | None):
    """Returns (console_out, output_path), BACK, or None (cancel).

    `default_path` seeds the custom-path prompt so the user can edit an
    already-provided value instead of retyping it.
    """
    choices = [
        questionary.Choice("📁 Save to reports/ (default, auto-named)", value=("file", None)),
        questionary.Choice("📝 Save to custom path…", value=("custom", None)),
        questionary.Choice("🖥  Print to terminal (rendered markdown)", value=("console", None)),
        questionary.Separator(),
        questionary.Choice("← Back", value=(BACK, None)),
    ]
    picked = await _bind_escape(
        questionary.select(
            "Where do you want the output?",
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
        _replace_last_line("[dim]← Back[/]")
        return BACK
    if action == "console":
        _replace_last_line("[bold cyan]?[/] output: [bold]console[/]")
        return True, None
    if action == "file":
        _replace_last_line("[bold cyan]?[/] output: [bold]reports/[/] [dim](auto-named)[/]")
        return False, None
    # Custom path — prompt for the exact path.
    seed = str(default_path) if default_path else ""
    raw = await questionary.text(
        "Custom output path (file or directory; blank = cancel)",
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
    _replace_last_line(f"[bold cyan]?[/] output: [bold]{path}[/]")
    return False, path


async def _pick_enrich() -> list[str] | None | object:
    """Pick which media kinds to enrich this run.

    Returns a list of enabled kind names (possibly empty = "none"),
    BACK to step back, or None to cancel. Pre-checks the current config
    defaults so the common case is "hit Enter".
    """
    settings = get_settings()
    cfg = settings.enrich
    # Order reflects default-on status: voice + videonote + link first
    # (the three kinds that default to True in config), then the opt-in
    # media enrichments. Keeping default-on items at the top means hitting
    # Enter through the wizard mostly picks the pre-checked defaults and
    # the user sees what's on without scrolling.
    all_kinds = [
        ("voice", "Voice messages — transcribe", cfg.voice),
        ("videonote", "Video notes (round videos) — transcribe", cfg.videonote),
        ("link", "External URLs — fetch and summarize", cfg.link),
        ("video", "Videos — transcribe audio track", cfg.video),
        ("image", "Photos — describe via vision model (spendy)", cfg.image),
        ("doc", "Documents (PDF / DOCX / text) — extract text", cfg.doc),
    ]
    choices = [
        questionary.Choice(title=label, value=key, checked=default_on) for key, label, default_on in all_kinds
    ]
    picked = await _bind_escape(
        _bind_arrow_checkbox(
            questionary.checkbox(
                "Enrich media? (→ check, ← uncheck, space to toggle, Enter to accept)",
                choices=choices,
                style=LIST_STYLE,
            )
        ),
        BACK,
    ).ask_async()
    if picked is None:
        return None
    if picked is BACK:
        _replace_last_line("[dim]← Back[/]")
        return BACK
    summary = ",".join(picked) if picked else "none"
    _replace_last_line(f"[bold cyan]?[/] enrich: [bold]{summary}[/]")
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
            "Message link or msg_id "
            "(e.g. https://t.me/c/1234567/890, https://t.me/somegroup/890, or bare 890 — "
            "blank to cancel)",
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
            console.print(f"[red]Can't parse '{raw}':[/] {e}")
            continue
        if parsed.msg_id is None:
            console.print(
                f"[red]No msg_id in '{raw}'.[/] "
                "Expected a message link like https://t.me/c/<chat>/<msg> "
                "(optionally with a topic in between), or a bare integer id."
            )
            continue
        return raw


async def _pick_mark_read(*, default: bool):
    """Yes/No/Back. Returns True, False, BACK, or None (cancel).

    Yes is listed first since it's the wizard default: after analyzing
    unread messages the user has effectively seen them, so advancing
    Telegram's read marker matches intent.
    """
    choices = [
        questionary.Choice("Yes — advance Telegram's read marker after analysis", value=True),
        questionary.Choice("No — keep messages unread in Telegram", value=False),
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


async def _pick_period(
    *,
    counts: dict[str, int | None] | None = None,
):
    """Returns (period_key, since, until, from_msg), BACK, or None.

    `counts` is an optional per-period message-count hint; if given, each
    choice is annotated with the count so the user can see how much work
    they're about to buy. `from_msg` is populated only when the user picks
    "From message" — otherwise it's None.

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
        return f"{base}  [{n} msgs]"

    options: list[Any] = [
        questionary.Choice(
            title=_label("Unread (default) — since Telegram read marker", "unread"),
            value="unread",
        ),
    ]
    options.extend(
        [
            questionary.Choice(title=_label("Last 7 days", "last7"), value="last7"),
            questionary.Choice(title=_label("Last 30 days", "last30"), value="last30"),
            questionary.Choice(title=_label("Full history", "full"), value="full"),
            questionary.Choice(title="From a specific message (link or id)…", value="from_msg"),
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
        "from_msg": "from a specific message",
    }
    label = _period_labels.get(key, key)
    n = c.get(key) if isinstance(key, str) else None
    label_with_count = f"{label} [{n} msgs]" if n is not None else label
    _replace_last_line(f"[bold cyan]?[/] period: [bold]{label_with_count}[/]")
    if key == "from_msg":
        ref = await _prompt_msg_ref()
        if ref is None:
            # Cancelled the sub-prompt → bounce back to the period picker
            # rather than the whole wizard. Gives the user a way out of
            # "I meant to pick last-7" without losing their chat / preset
            # choice so far.
            return await _pick_period(counts=counts)
        _replace_last_line(f"[bold cyan]?[/] period: [bold]from {ref}[/]")
        return key, None, None, ref
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
                    return await _pick_period(counts=counts)
        return key, since or None, until or None, None
    return key, None, None, None


__all__ = [
    "ALL_UNREAD",
    "BACK",
    "InteractiveAnswers",
    "Path",
    "build_analyze_args",
    "build_dump_args",
    "run_interactive_analyze",
    "run_interactive_describe",
    "run_interactive_dump",
]
