"""`atg chats run` — walk every enabled subscription, sync + analyze each.

Each subscription stores its own `preset`, `period`, `enrich_kinds`,
`mark_read`, and `post_to` (set during `atg chats add`). `atg chats run` walks
the enabled list and dispatches each one through the same machinery as
`atg analyze` would. Global flags on the command override the per-sub
value for that run only — handy for "run everyone with `--preset
action_items` today".

Comments subs (`source_kind == "comments"`) are skipped here: their
content is pulled inline by their parent channel's `--with-comments`
auto-detection (see `core/pipeline._pull_linked_comments`). Running
them on their own would double-analyze the same group.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.i18n import t as _t
from analyzetg.i18n import tf as _tf
from analyzetg.models import Subscription
from analyzetg.tg.client import tg_client
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _resolve_period(sub_value: str, override: str | None) -> str:
    """Pick the period to use for one subscription's run.

    Empty / "default" override falls back to the sub's stored value;
    otherwise the override wins. Validation upstream — we accept any
    value here so `cmd_run` can pass through.
    """
    return override or sub_value or "unread"


def _period_to_kwargs(period: str) -> dict[str, Any]:
    """Translate the wizard's period key into cmd_analyze kwargs."""
    if period == "last7":
        return {"last_days": 7, "full_history": False}
    if period == "last30":
        return {"last_days": 30, "full_history": False}
    if period == "full":
        return {"full_history": True}
    # "unread" / unknown → no flags, defaults to read-marker semantics.
    return {"full_history": False}


def _resolve_enrich(
    sub_value: str | None, override: str | None, override_all: bool, override_none: bool
) -> dict[str, Any]:
    """Map per-sub stored enrich + run-time overrides to cmd_analyze flags.

    Precedence: `--no-enrich` > `--enrich-all` > `--enrich CSV` > sub's
    stored `enrich_kinds`. When neither sub nor override pins the
    setting, falls back to config defaults (cmd_analyze handles that).
    """
    if override_none:
        return {"enrich": None, "enrich_all": False, "no_enrich": True}
    if override_all:
        return {"enrich": None, "enrich_all": True, "no_enrich": False}
    if override is not None:
        return {"enrich": override, "enrich_all": False, "no_enrich": False}
    if sub_value is not None:
        # Empty string on the sub means "explicitly disable everything".
        if sub_value == "":
            return {"enrich": None, "enrich_all": False, "no_enrich": True}
        return {"enrich": sub_value, "enrich_all": False, "no_enrich": False}
    # No setting anywhere → cmd_analyze picks up config defaults.
    return {"enrich": None, "enrich_all": False, "no_enrich": False}


async def _has_linked_comments_sub(repo, channel_chat_id: int, all_subs: list[Subscription]) -> bool:
    """Return True when this channel has a sibling `comments` sub.

    Used to auto-pass `--with-comments` for the channel's analyze run so
    `atg chats run` matches the user's stated intent at `chats add` time.
    """
    chat_row = await repo.get_chat(channel_chat_id)
    linked = (chat_row or {}).get("linked_chat_id")
    if linked is None:
        return False
    return any(int(s.chat_id) == int(linked) and s.source_kind == "comments" for s in all_subs)


async def cmd_run(
    *,
    only_chat: int | None = None,
    preset_override: str | None = None,
    period_override: str | None = None,
    enrich_override: str | None = None,
    enrich_all_override: bool = False,
    no_enrich_override: bool = False,
    mark_read_override: bool | None = None,
    post_to_override: str | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    flat: bool = False,
    yes: bool = False,
) -> None:
    """Walk every enabled subscription, run analyze on each.

    Skips comments subs (their parent channel pulls them inline via
    `--with-comments` when a sibling sub is present). Reports a
    per-sub status line and a final summary table.

    `flat=True` switches to a single combined analysis across every
    enabled sub: messages from all chats are merged into one input,
    rendered with the per-chat `chat_groups` formatter, and analyzed
    in one map-reduce pass. The result is a single multi-chat report
    in `reports/run-flat-<ts>.md`. In flat mode, per-sub stored
    settings are ignored; the run uses the CLI overrides plus
    `summary` / `unread` / config defaults.
    """
    # Mode picker: when neither `--flat` nor `--yes` is on the CLI, show
    # the subscriptions list and ask interactively which mode to run.
    # `--flat` (or `--yes` for unattended runs) skips the picker and
    # goes straight to the chosen mode. Cancel exits cleanly.
    if not flat and not yes:
        # Brief peek at the worklist so the picker has context. Open
        # the repo just to read; the per-chat / flat dispatchers each
        # re-open it themselves.
        settings = get_settings()
        async with open_repo(settings.storage.data_path) as repo:
            all_subs = await repo.list_subscriptions(enabled_only=True)
        if only_chat is not None:
            all_subs = [s for s in all_subs if int(s.chat_id) == int(only_chat)]
        targets_preview = [s for s in all_subs if s.source_kind != "comments"]
        if not targets_preview:
            console.print(f"[yellow]{_t('run_no_enabled_subs')}[/]")
            return

        # Compact summary so the user can see what's about to be hit
        # without scrolling. One row per non-comments sub; the comments
        # column flags channel+comments pairs the same way the per-chat
        # plan table will.
        async with open_repo(settings.storage.data_path) as repo:
            n_comments_pairs = 0
            comments_for: dict[int, bool] = {}
            for s in targets_preview:
                if s.source_kind == "channel":
                    has = await _has_linked_comments_sub(repo, int(s.chat_id), all_subs)
                    comments_for[int(s.chat_id)] = has
                    if has:
                        n_comments_pairs += 1
        summary_table = Table(title=_tf("run_summary_table_title", n=len(targets_preview)))
        for col_key in (
            "run_col_title",
            "run_col_kind",
            "run_col_preset",
            "run_col_period",
            "run_col_comments",
        ):
            summary_table.add_column(_t(col_key))
        for s in targets_preview:
            comments_label = _t("run_folded_in_label") if comments_for.get(int(s.chat_id)) else _t("run_dash")
            summary_table.add_row(
                (s.title or str(s.chat_id))[:50],
                s.source_kind,
                s.preset or _t("run_default_preset"),
                s.period or _t("run_default_period"),
                comments_label,
            )
        console.print(summary_table)
        if n_comments_pairs:
            console.print(f"[dim]{_tf('run_comments_auto_merge', n=n_comments_pairs)}[/]")

        # Use LIST_STYLE + ESC-binding for consistency with the analyze
        # wizard. ESC = cancel; mouse-friendly choice labels.
        try:
            import questionary

            from analyzetg.interactive import LIST_STYLE, _bind_escape

            mode = await _bind_escape(
                questionary.select(
                    _t("run_mode_picker_q"),
                    choices=[
                        questionary.Choice(title=_t("run_mode_per_chat"), value="per_chat"),
                        questionary.Choice(title=_t("run_mode_flat"), value="flat"),
                        questionary.Separator(),
                        questionary.Choice(title=_t("run_mode_cancel"), value="cancel"),
                    ],
                    default="per_chat",
                    use_jk_keys=False,
                    style=LIST_STYLE,
                ),
                "cancel",
            ).ask_async()
        except (KeyboardInterrupt, EOFError):
            mode = "cancel"
        if mode is None or mode == "cancel":
            console.print(f"[dim]{_t('cancelled')}[/]")
            return
        if mode == "flat":
            flat = True

    if flat:
        await _cmd_run_flat(
            only_chat=only_chat,
            preset_override=preset_override,
            period_override=period_override,
            enrich_override=enrich_override,
            enrich_all_override=enrich_all_override,
            no_enrich_override=no_enrich_override,
            mark_read_override=mark_read_override,
            post_to_override=post_to_override,
            max_cost=max_cost,
            dry_run=dry_run,
            yes=yes,
        )
        return
    settings = get_settings()
    async with tg_client(settings) as _, open_repo(settings.storage.data_path) as repo:
        all_subs = await repo.list_subscriptions(enabled_only=True)
        if only_chat is not None:
            all_subs = [s for s in all_subs if int(s.chat_id) == int(only_chat)]
        # Comments subs ride along with their parent channel, never on
        # their own — drop them from the worklist.
        targets = [s for s in all_subs if s.source_kind != "comments"]
        if not targets:
            console.print(f"[yellow]{_t('run_no_enabled_subs')}[/]")
            return

        # Pre-resolve per-target `with_comments` so the plan table can
        # show "+ comments" before the user confirms. Also reuses these
        # values inside the per-sub loop below — no second lookup needed.
        with_comments_map: dict[tuple[int, int], bool] = {}
        for s in targets:
            if s.source_kind == "channel":
                with_comments_map[(int(s.chat_id), int(s.thread_id))] = await _has_linked_comments_sub(
                    repo, int(s.chat_id), all_subs
                )
            else:
                with_comments_map[(int(s.chat_id), int(s.thread_id))] = False

        # Plan summary first so the user can confirm before any
        # backfill / OpenAI spend.
        plan = Table(title=_tf("run_plan_title", n=len(targets)))
        for col_key in (
            "run_col_chat_id",
            "run_col_title",
            "run_col_preset",
            "run_col_period",
            "run_col_enrich",
            "run_col_mark_read",
            "run_col_post_to",
            "run_col_comments",
        ):
            plan.add_column(_t(col_key))
        for s in targets:
            preset_eff = preset_override or s.preset or _t("run_default_preset")
            period_eff = _resolve_period(s.period, period_override)
            enrich_eff = _resolve_enrich(
                s.enrich_kinds, enrich_override, enrich_all_override, no_enrich_override
            )
            if enrich_eff["no_enrich"]:
                enrich_label = _t("run_enrich_none")
            elif enrich_eff["enrich_all"]:
                enrich_label = _t("run_enrich_all")
            elif enrich_eff["enrich"]:
                enrich_label = enrich_eff["enrich"]
            else:
                enrich_label = _t("run_enrich_config_defaults")
            mr_eff = mark_read_override if mark_read_override is not None else s.mark_read
            pt_eff = post_to_override or s.post_to or _t("run_dash")
            comments_label = (
                _t("run_folded_in_label")
                if with_comments_map[(int(s.chat_id), int(s.thread_id))]
                else _t("run_dash")
            )
            plan.add_row(
                str(s.chat_id),
                (s.title or "")[:40],
                preset_eff,
                period_eff,
                enrich_label,
                _t("wiz_summary_yes") if mr_eff else _t("wiz_summary_no"),
                pt_eff,
                comments_label,
            )
        console.print(plan)
        # Spell out the channel+comments behaviour up front — easy to miss
        # otherwise that one channel + one comments sub = one merged report.
        n_with_comments = sum(1 for v in with_comments_map.values() if v)
        if n_with_comments:
            console.print(f"[dim]{_tf('run_comments_merge_note', n=n_with_comments)}[/]")
        if dry_run:
            console.print(f"[dim]{_t('run_dry_run_note')}[/]")
            return
        if not yes and not typer.confirm(_tf("run_analyze_confirm_q", n=len(targets)), default=True):
            console.print(f"[dim]{_t('cancelled')}[/]")
            return

    # Re-open client/repo per sub via cmd_analyze (each opens its own).
    # Aggregate results for the final summary.
    results: list[dict[str, Any]] = []
    from analyzetg.analyzer.commands import cmd_analyze

    for i, s in enumerate(targets, start=1):
        preset_eff = preset_override or s.preset or "summary"
        period_eff = _resolve_period(s.period, period_override)
        enrich_eff = _resolve_enrich(s.enrich_kinds, enrich_override, enrich_all_override, no_enrich_override)
        period_kwargs = _period_to_kwargs(period_eff)
        mr_eff = mark_read_override if mark_read_override is not None else s.mark_read
        pt_eff = post_to_override or s.post_to

        # Use the pre-resolved value so we don't re-open the repo per sub.
        with_comments = with_comments_map.get((int(s.chat_id), int(s.thread_id)), False)

        title = s.title or str(s.chat_id)
        progress = _tf(
            "run_progress_line",
            i=i,
            total=len(targets),
            title=title,
            preset=preset_eff,
            period=period_eff,
        )
        console.print(f"\n[bold cyan]>>[/] {progress}")
        try:
            await cmd_analyze(
                ref=str(s.chat_id),
                thread=int(s.thread_id) if s.thread_id else None,
                msg=None,
                from_msg=None,
                full_history=period_kwargs.get("full_history", False),
                since=None,
                until=None,
                last_days=period_kwargs.get("last_days"),
                preset=preset_eff,
                prompt_file=None,
                model=None,
                filter_model=None,
                output=None,
                console_out=False,
                save_default=True,  # auto-save to reports/<chat>/...
                mark_read=mr_eff,
                no_cache=False,
                include_transcripts=True,
                min_msg_chars=None,
                **enrich_eff,
                all_flat=False,
                all_per_topic=False,
                folder=None,
                max_cost=max_cost,
                post_saved=False,
                dry_run=False,
                cite_context=0,
                self_check=False,
                by=None,
                post_to=pt_eff,
                repeat_last=False,
                with_comments=with_comments,
                yes=True,  # plan already confirmed; per-sub re-prompt would be wrong
            )
            results.append({"chat_id": s.chat_id, "title": title, "ok": True, "err": None})
        except typer.Exit as e:
            # Exit(0) inside cmd_analyze means "nothing to analyze for
            # this sub" (e.g. zero unread); not a failure of the run as
            # a whole. Higher exit codes propagate as a per-sub error.
            if e.exit_code == 0:
                results.append(
                    {"chat_id": s.chat_id, "title": title, "ok": True, "err": _t("run_skipped_no_msgs")}
                )
            else:
                results.append(
                    {
                        "chat_id": s.chat_id,
                        "title": title,
                        "ok": False,
                        "err": _tf("run_exit_code_label", code=e.exit_code),
                    }
                )
        except Exception as e:
            log.error("run.sub_failed", chat_id=s.chat_id, err=str(e)[:300])
            results.append({"chat_id": s.chat_id, "title": title, "ok": False, "err": str(e)[:200]})

    # Final summary.
    summary = Table(title=_t("run_results_title"))
    for col_key in ("run_col_chat_id", "run_col_title", "run_col_status", "run_col_note"):
        summary.add_column(_t(col_key))
    ok_count = 0
    for r in results:
        status = f"[green]{_t('run_status_ok')}[/]" if r["ok"] else f"[red]{_t('run_status_fail')}[/]"
        if r["ok"]:
            ok_count += 1
        summary.add_row(str(r["chat_id"]), r["title"][:40], status, r["err"] or "")
    console.print(summary)
    console.print(f"[bold]{_tf('run_results_summary', ok=ok_count, total=len(results))}[/]")


async def _cmd_run_flat(
    *,
    only_chat: int | None,
    preset_override: str | None,
    period_override: str | None,
    enrich_override: str | None,
    enrich_all_override: bool,
    no_enrich_override: bool,
    mark_read_override: bool | None,
    post_to_override: str | None,
    max_cost: float | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """One combined analysis across every enabled subscription.

    For each sub: prepare_chat_run pulls + enriches its messages;
    everything is merged into a single list and analyzed once, with
    `chat_groups` so each chat keeps its own header + citation
    template in the report.
    """
    from datetime import datetime as _dt

    from analyzetg.analyzer.formatter import build_link_template
    from analyzetg.analyzer.pipeline import AnalysisOptions, run_analysis
    from analyzetg.analyzer.prompts import PRESETS
    from analyzetg.config import get_settings as _get_settings
    from analyzetg.core.paths import derive_internal_id
    from analyzetg.core.pipeline import prepare_chat_run
    from analyzetg.enrich.base import EnrichOpts

    settings = _get_settings()

    # In flat mode, per-sub stored settings get ignored: the merged
    # report needs ONE preset / period / enrich. CLI overrides win;
    # otherwise default to `multichat` (purpose-built for the cross-chat
    # scan use case — section per chat plus cross-chat themes), period
    # "unread", and config-default enrichments.
    preset_name = preset_override or "multichat"
    if preset_name not in PRESETS:
        console.print(f"[red]{_t('run_unknown_preset')}[/] {preset_name}")
        raise typer.Exit(2)
    period = period_override or "unread"
    period_kwargs = _period_to_kwargs(period)
    enrich_dict = _resolve_enrich(None, enrich_override, enrich_all_override, no_enrich_override)

    # Build EnrichOpts from the resolved settings. Mirrors the kwargs
    # cmd_analyze would assemble via build_enrich_opts; we do it
    # ourselves here because flat mode bypasses cmd_analyze entirely.
    if enrich_dict["no_enrich"]:
        enrich_opts = EnrichOpts()
    elif enrich_dict["enrich_all"]:
        enrich_opts = EnrichOpts(voice=True, videonote=True, video=True, image=True, doc=True, link=True)
    elif enrich_dict["enrich"]:
        kinds = {k.strip() for k in enrich_dict["enrich"].split(",") if k.strip()}
        enrich_opts = EnrichOpts(
            voice="voice" in kinds,
            videonote="videonote" in kinds,
            video="video" in kinds,
            image="image" in kinds,
            doc="doc" in kinds,
            link="link" in kinds,
        )
    else:
        cfg = settings.enrich
        enrich_opts = EnrichOpts(
            voice=cfg.voice,
            videonote=cfg.videonote,
            video=cfg.video,
            image=cfg.image,
            doc=cfg.doc,
            link=cfg.link,
        )

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        all_subs = await repo.list_subscriptions(enabled_only=True)
        if only_chat is not None:
            all_subs = [s for s in all_subs if int(s.chat_id) == int(only_chat)]
        targets = [s for s in all_subs if s.source_kind != "comments"]
        if not targets:
            console.print(f"[yellow]{_t('run_no_enabled_subs')}[/]")
            return

        # Plan summary.
        plan = Table(title=_tf("run_flat_plan_title", n=len(targets)))
        for col_key in ("run_col_chat_id", "run_col_title", "run_col_kind"):
            plan.add_column(_t(col_key))
        for s in targets:
            plan.add_row(str(s.chat_id), (s.title or "")[:50], s.source_kind)
        console.print(plan)
        if enrich_dict["enrich_all"]:
            enrich_summary = _t("run_enrich_all")
        elif enrich_dict["no_enrich"]:
            enrich_summary = _t("run_enrich_none")
        else:
            enrich_summary = enrich_dict["enrich"] or _t("run_enrich_config_defaults")
        console.print(
            f"[dim]{_tf('run_flat_mode_desc', preset=preset_name, period=period, enrich=enrich_summary)}[/]"
        )
        if dry_run:
            console.print(f"[dim]{_t('run_dry_run_note')}[/]")
            return
        if not yes and not typer.confirm(_tf("run_flat_confirm_q", n=len(targets)), default=True):
            console.print(f"[dim]{_t('cancelled')}[/]")
            return

        # Pull each sub's messages. For channels with a sibling comments
        # sub, prepare_chat_run handles `with_comments=True` so the
        # comments get pulled in alongside the channel posts — they
        # land in `prepared.messages` with their original chat_id and
        # render in their own group inside the merged report.
        all_messages: list = []
        chat_groups: dict[int, dict] = {}
        # Per-sub breakdown: each entry tracks how many messages came
        # from the primary chat (channel posts) vs the linked
        # discussion group (comments). Surfaces in the report header
        # so the reader sees "12 channel + 244 comments" instead of
        # one opaque "256 msg" lump.
        per_sub_msg_count: list[dict[str, Any]] = []
        marks: list = []  # mark_read closures to fire after a successful run

        for i, s in enumerate(targets, start=1):
            with_comments = False
            if s.source_kind == "channel":
                with_comments = await _has_linked_comments_sub(repo, int(s.chat_id), all_subs)
            mr_eff = mark_read_override if mark_read_override is not None else s.mark_read
            maybe_comments = _t("run_flat_with_comments_suffix") if with_comments else ""
            console.print(
                f"[bold cyan]>>[/] "
                f"{_tf('run_flat_sub_progress', i=i, total=len(targets), title=s.title or s.chat_id, kind=s.source_kind, maybe_comments=maybe_comments)}"
            )
            try:
                prepared = await prepare_chat_run(
                    client=client,
                    repo=repo,
                    settings=settings,
                    chat_id=int(s.chat_id),
                    thread_id=int(s.thread_id) if s.thread_id else None,
                    chat_title=s.title,
                    chat_username=None,  # resolver hasn't run; the link template still works without it
                    chat_internal_id=derive_internal_id(int(s.chat_id)),
                    full_history=period_kwargs.get("full_history", False),
                    from_msg_id=None,
                    since_dt=None,
                    until_dt=None,
                    enrich_opts=enrich_opts,
                    include_transcripts=True,
                    mark_read=mr_eff,
                    with_comments=with_comments,
                )
            except typer.Exit as e:
                if e.exit_code == 0:
                    console.print(f"[dim]{_tf('run_flat_no_msgs', title=s.title or s.chat_id)}[/]")
                    continue
                raise

            if not prepared.messages:
                console.print(f"[dim]{_tf('run_flat_zero_msgs', title=s.title or s.chat_id)}[/]")
                continue

            # Last_days lives on the iter-level filter rather than
            # backfill, so apply it post-iter (cheaper than another
            # call to prepare with since_dt).
            if period_kwargs.get("last_days"):
                cutoff = _dt.utcnow().timestamp() - period_kwargs["last_days"] * 86400
                prepared.messages[:] = [m for m in prepared.messages if m.date.timestamp() >= cutoff]

            all_messages.extend(prepared.messages)
            # Split the primary-vs-comments counts by chat_id so the
            # report header can attribute messages correctly. Comments
            # rows live under prepared.comments_chat_id (when present);
            # everything else is the primary chat's own.
            primary_n = sum(1 for m in prepared.messages if int(m.chat_id) == int(s.chat_id))
            comments_n = len(prepared.messages) - primary_n
            per_sub_msg_count.append(
                {
                    "chat_id": int(s.chat_id),
                    "title": s.title or str(s.chat_id),
                    "kind": s.source_kind,
                    "primary_count": primary_n,
                    "comments_count": comments_n,
                    "comments_title": prepared.comments_chat_title,
                    "comments_chat_id": prepared.comments_chat_id,
                }
            )
            chat_groups[int(s.chat_id)] = {
                "title": s.title or str(s.chat_id),
                "link_template": build_link_template(
                    chat_username=None,
                    chat_internal_id=derive_internal_id(int(s.chat_id)),
                    thread_id=int(s.thread_id) if s.thread_id else None,
                ),
            }
            if with_comments and prepared.comments_chat_id is not None:
                chat_groups[int(prepared.comments_chat_id)] = {
                    "title": prepared.comments_chat_title
                    or _tf("run_flat_comments_fallback_title", chat_id=prepared.comments_chat_id),
                    "link_template": build_link_template(
                        chat_username=prepared.comments_chat_username,
                        chat_internal_id=prepared.comments_chat_internal_id,
                        thread_id=None,
                    ),
                }
            if prepared.mark_read_fn is not None:
                marks.append(prepared.mark_read_fn)

        if not all_messages:
            console.print(f"[yellow]{_t('run_no_msgs_across_subs')}[/]")
            return

        # One merged analysis.
        opts = AnalysisOptions(
            preset=preset_name,
            include_transcripts=True,
            enrich=enrich_opts,
        )
        title = _tf(
            "run_flat_title",
            n_chats=len(per_sub_msg_count),
            n_msgs=len(all_messages),
        )
        console.print(f"\n[dim]{_tf('run_flat_analyzing', n=len(all_messages))}[/]")
        try:
            result = await run_analysis(
                repo=repo,
                chat_id=0,  # synthetic id for the merged run; not stored in cache key in a meaningful way
                thread_id=None,
                title=title,
                opts=opts,
                chat_username=None,
                chat_internal_id=None,
                client=client,
                topic_titles=None,
                topic_markers=None,
                messages=all_messages,
                chat_groups=chat_groups,
            )
        except Exception as e:
            log.error("run.flat_failed", err=str(e)[:300])
            console.print(f"[red]{_t('run_flat_failed')}[/] {e}")
            raise

        # Save to a single timestamped file.
        from pathlib import Path

        ts = _dt.now().strftime("%Y-%m-%d_%H-%M")
        out_dir = Path("reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"run-flat-{ts}.md"
        cost_str = f"{float(result.total_cost_usd or 0):.4f}"
        body_lines = [
            _tf("run_flat_report_h1", n_chats=len(per_sub_msg_count)),
            "",
            _tf(
                "run_flat_report_meta",
                preset=preset_name,
                period=period,
                n_msgs=len(all_messages),
                cost=cost_str,
            ),
            "",
            _t("run_flat_per_chat_h2"),
            "",
        ]
        for item in per_sub_msg_count:
            primary_label = _t("run_flat_kind_channel") if item["kind"] == "channel" else item["kind"]
            counts_bits = [f"{item['primary_count']} {primary_label}"]
            if item["comments_count"]:
                comments_title = item["comments_title"] or _tf(
                    "run_flat_comments_fallback_title", chat_id=item["comments_chat_id"]
                )
                counts_bits.append(
                    _tf("run_flat_comments_label", n=item["comments_count"], title=comments_title)
                )
            body_lines.append(f"- **{item['title']}** (`{item['chat_id']}`): {' + '.join(counts_bits)}")
        body_lines.append("")
        body_lines.append("---")
        body_lines.append("")
        body_lines.append(result.final_result)
        out_path.write_text("\n".join(body_lines), encoding="utf-8")
        console.print(f"[green]{_t('run_saved_label')}[/] {out_path}")

        # Optionally post to a chat.
        if post_to_override:
            from analyzetg.analyzer.commands import _post_to_chat

            try:
                await _post_to_chat(
                    client,
                    repo,
                    result,
                    title=title,
                    target=post_to_override,
                )
            except Exception as e:
                console.print(f"[yellow]{_t('run_post_to_failed')}[/] {e}")

        # Mark read for each sub the user opted in for.
        marked = 0
        for fn in marks:
            try:
                marked += await fn() or 0
            except Exception as e:
                log.warning("run.flat.mark_read_failed", err=str(e)[:200])
        if marked:
            console.print(f"[dim]{_tf('run_marked_read_across', n=marked)}[/]")


__all__ = ["cmd_run"]
