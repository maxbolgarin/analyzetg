"""`unread prompt "..."` — direct chat with the configured AI provider.

Bypasses every source-pipeline layer (no retrieval, no preset, no
Telegram session, no archive context). The only context the LLM sees is
an optional one-line "Respond in <report_language>." system message so
the user can pin the answer language without typing it into the prompt
itself. Cost still flows through `chat_complete` so usage_log gets a
`phase=prompt` row and `unread stats --by kind` surfaces it for free.

After the first answer the loop offers a "Continue chatting?" prompt
(unless `--no-followup` is set) and turns into a multi-turn conversation
with the same shape as `unread ask` — `_ask_continue` and
`_build_history_messages` are reused from `unread.ask.commands`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from unread.ai.providers import _resolve_provider_name, make_chat_provider, resolve_chat_model
from unread.analyzer.openai_client import chat_complete
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf

console = Console()


def _chat_slot_provider_has_key(settings, provider: str) -> bool:  # type: ignore[no-untyped-def]
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


def _build_messages(
    system: str,
    history: list[tuple[str, str]],
    new_user_text: str,
) -> list[dict[str, str]]:
    """system → (user, assistant)*N → user, with system optional.

    Mirrors `ask.commands._build_history_messages` but allows an empty
    system line — for a raw prompt with no answer-language hint we want
    nothing in front of the first user turn.
    """
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for q, a in history:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": new_user_text})
    return msgs


async def cmd_prompt(
    *,
    prompt: str,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    report_language: str | None = None,
    max_tokens: int = 2000,
    max_cost: float | None = None,
    yes: bool = False,
    no_followup: bool = False,
) -> str:
    """Send `prompt` straight to the active chat provider, return the FIRST answer text.

    `report_language` (or `settings.locale.report_language`) controls
    the answer language via a single `Respond in <lang>.` system line —
    omitted entirely when neither is set, so the LLM auto-detects from
    the prompt. `--output` saves a markdown file (first turn only);
    default is terminal render. `--console` forces terminal even when
    `--output` is set.

    `no_followup=True` skips the post-answer "Continue chatting?"
    keypress (use in scripts / cron / non-interactive). On a TTY with
    `no_followup=False`, the loop turns into multi-turn chat with the
    same UX as `unread ask`: Enter / `y` continues, `n` / Esc / Ctrl-D
    exits; follow-up turns always render to terminal.
    """
    settings = get_settings()
    provider_name = _resolve_provider_name(settings, "chat")
    if not _chat_slot_provider_has_key(settings, provider_name):
        from unread.cli import _print_first_run_banner

        _print_first_run_banner("openai" if provider_name == "openai" else "ai")
        raise typer.Exit(1)

    used_model = (model or "").strip() or resolve_chat_model(settings)
    answer_lang = (report_language or settings.locale.report_language or "").strip().lower()
    system_line = f"Respond in {answer_lang}." if answer_lang else ""

    provider = make_chat_provider(settings)

    async with open_repo(settings.storage.data_path) as repo:
        history: list[tuple[str, str]] = []

        async def _run_turn(user_text: str, *, save_to_file: bool) -> str:
            """One full turn: cost preview → chat_complete → render → optional save.

            `save_to_file` is True only for the first turn so a multi-turn
            session doesn't keep clobbering the same `--output` file with
            disconnected snippets.
            """
            messages = _build_messages(system_line, history, user_text)
            from unread.util.pricing import chat_cost
            from unread.util.tokens import count_tokens

            prompt_tokens = sum(count_tokens(m["content"], used_model) for m in messages)
            est_cost = chat_cost(used_model, prompt_tokens, 0, max_tokens, settings=settings)
            if est_cost is not None:
                console.print(
                    f"[grey70]→ Estimated cost: ~${est_cost:.4f}[/] "
                    f"({prompt_tokens:,} prompt tokens × {used_model}; output capped at {max_tokens})"
                )
                if max_cost is not None and est_cost > max_cost:
                    console.print(
                        f"[bold yellow]⚠ Estimated cost ${est_cost:.4f} exceeds --max-cost ${max_cost:.4f}.[/]"
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

            res = await chat_complete(
                provider,
                repo=repo,
                model=used_model,
                messages=messages,
                max_tokens=max_tokens,
                context={"phase": "prompt", "turn": len(history) + 1},
            )
            answer_text = (res.text or "").strip()
            if not answer_text:
                console.print("[red]Model returned empty output.[/]")
                raise typer.Exit(1)

            body = (
                f"# {user_text}\n\n_model {used_model}, ${float(res.cost_usd or 0):.4f}_\n\n{answer_text}\n"
            )
            # Render: terminal when no --output, OR when --console forces it,
            # OR when we're past the first turn (a follow-up always shows
            # on screen — the saved file already captured turn 1).
            if console_out or output is None or not save_to_file:
                from rich.markdown import Markdown
                from rich.rule import Rule

                console.print(Rule("answer", style="cyan"))
                console.print(Markdown(body))
                console.print(Rule(style="cyan"))
            if output is not None and save_to_file:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(body, encoding="utf-8")
                from unread.util.fsmode import tighten

                tighten(output)
                console.print(f"[green]{_tf('saved_to_path', path=output)}[/]")
            return answer_text

        first_answer = await _run_turn(prompt, save_to_file=True)
        history.append((prompt, first_answer))

        if no_followup:
            return first_answer

        # Reuse the ask flow's "Continue chatting?" keypress so the UX is
        # identical across the two entry points. `_ask_continue` returns
        # False on EOF / Ctrl-C — same as the user typing `n`.
        from unread.ask.commands import _ask_continue

        try:
            cont = await _ask_continue()
        except (EOFError, KeyboardInterrupt):
            cont = False
        if not cont:
            return first_answer

        # Multi-turn loop. prompt_toolkit handles non-ASCII input and
        # plays nicely with asyncio (plain `input()` corrupts Cyrillic
        # entry inside the event loop on macOS — same bug ask hit).
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings

        console.print("\n[bold cyan]Interactive mode[/] — type a follow-up (Esc / blank / Ctrl-D to exit).")
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
                ans = await _run_turn(follow, save_to_file=False)
            except typer.Exit as e:
                # Budget-guard abort on a single follow-up = "skip this
                # turn", not "kill the session". Same convention as ask.
                if e.exit_code == 0:
                    continue
                raise
            history.append((follow, ans))

        return first_answer
