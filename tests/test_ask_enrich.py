"""ask + enrich: wizard step, inline flag, and pre-retrieval enrichment.

Three layers covered:

1. **Inline flags** (`unread ask "Q" @chat --enrich voice,image`) reach
   `cmd_ask` and trigger `enrich_messages` BEFORE retrieval — same
   helper analyze uses (`build_enrich_opts` + `enrich_messages`),
   not a re-implementation. Order matters: enrichment first so
   transcripts/captions/etc. become searchable in the same run.

2. **Wizard dispatch**: `run_interactive_ask` forwards
   `answers.enrich_kinds` through `_build_enrich_kwargs` to `cmd_ask`
   as `--enrich=<csv>` / `--enrich-all` / `--no-enrich`.

3. **Defaults pass-through**: `enrich_kinds=None` (wizard never asked
   or user declined to override) means "use config defaults" — the
   wizard MUST NOT force `--no-enrich` in that case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_ask_inline_enrich_runs_before_retrieval():
    """`unread ask "Q" @chat --enrich voice` runs enrichment before retrieval.

    Spec: enrichment must occur after scope resolution but before the
    retrieval step, so voice transcripts (and image descriptions, etc.)
    become searchable mid-run.
    """
    from unread.ask import commands as ask_commands

    order: list[str] = []

    async def fake_enrich(*args, **kwargs):
        order.append("enrich")
        # Mirror the real enrich_messages return shape.
        from unread.enrich.base import EnrichStats

        return EnrichStats()

    async def fake_run_single_turn(*args, **kwargs):
        order.append("retrieve")
        return ("answer", [])

    # Resolve returns a chat_id only; thread_id/msg_id stay None.
    async def fake_resolve(client, repo, ref):
        order.append("resolve")
        return MagicMock(chat_id=42, thread_id=None, msg_id=None, title="X", username="x")

    fake_repo = AsyncMock()
    fake_repo.iter_messages = AsyncMock(return_value=[MagicMock()])
    fake_repo.get_chat = AsyncMock(return_value={"username": "x", "title": "X"})

    fake_client = MagicMock()
    fake_settings = MagicMock()
    fake_settings.locale.language = "en"
    fake_settings.locale.content_language = ""
    fake_settings.openai.chat_model_default = "gpt-5.4-mini"
    fake_settings.ask.rerank_enabled = False
    fake_settings.storage.data_path = "/tmp/test.sqlite"

    @asyncmgr_factory(fake_client)
    class _TgCtx:
        pass

    @asyncmgr_factory(fake_repo)
    class _RepoCtx:
        pass

    with (
        patch.object(ask_commands, "tg_client", lambda *a, **kw: _TgCtx()),
        patch.object(ask_commands, "open_repo", lambda *a, **kw: _RepoCtx()),
        patch.object(ask_commands, "get_settings", return_value=fake_settings),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single_turn),
        patch("unread.enrich.pipeline.enrich_messages", new=fake_enrich),
        # Skip the post-answer "Continue chatting?" prompt.
    ):
        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@somechat",
            folder=None,
            global_scope=False,
            enrich="voice",
            enrich_all=False,
            no_enrich=False,
            no_followup=True,
        )

    # Enrichment must come before the LLM retrieval/answer step.
    assert "enrich" in order, f"enrichment was never called: {order}"
    assert "retrieve" in order, f"retrieval was never called: {order}"
    assert order.index("enrich") < order.index("retrieve"), (
        f"enrichment ran AFTER retrieval; expected before. order={order}"
    )


@pytest.mark.asyncio
async def test_ask_no_enrich_skips_enrichment():
    """`unread ask --no-enrich` does NOT call enrich_messages."""
    from unread.ask import commands as ask_commands

    enrich_called = {"n": 0}

    async def fake_enrich(*args, **kwargs):
        enrich_called["n"] += 1
        from unread.enrich.base import EnrichStats

        return EnrichStats()

    async def fake_run_single_turn(*args, **kwargs):
        return ("answer", [])

    async def fake_resolve(client, repo, ref):
        return MagicMock(chat_id=42, thread_id=None, msg_id=None, title="X", username="x")

    fake_repo = AsyncMock()
    fake_repo.iter_messages = AsyncMock(return_value=[MagicMock()])
    fake_repo.get_chat = AsyncMock(return_value={"username": "x", "title": "X"})

    fake_client = MagicMock()
    fake_settings = MagicMock()
    fake_settings.locale.language = "en"
    fake_settings.locale.content_language = ""
    fake_settings.openai.chat_model_default = "gpt-5.4-mini"
    fake_settings.ask.rerank_enabled = False
    fake_settings.storage.data_path = "/tmp/test.sqlite"

    @asyncmgr_factory(fake_client)
    class _TgCtx:
        pass

    @asyncmgr_factory(fake_repo)
    class _RepoCtx:
        pass

    with (
        patch.object(ask_commands, "tg_client", lambda *a, **kw: _TgCtx()),
        patch.object(ask_commands, "open_repo", lambda *a, **kw: _RepoCtx()),
        patch.object(ask_commands, "get_settings", return_value=fake_settings),
        patch.object(ask_commands, "resolve_ref", new=fake_resolve),
        patch.object(ask_commands, "_run_single_turn", new=fake_run_single_turn),
        patch("unread.enrich.pipeline.enrich_messages", new=fake_enrich),
    ):
        await ask_commands.cmd_ask(
            question="hello",
            ref=None,
            chat="@somechat",
            folder=None,
            global_scope=False,
            enrich=None,
            enrich_all=False,
            no_enrich=True,
            no_followup=True,
        )

    assert enrich_called["n"] == 0, "--no-enrich must skip enrich_messages entirely"


@pytest.mark.asyncio
async def test_run_interactive_ask_forwards_enrich_kinds_to_cmd_ask():
    """Wizard `enrich_kinds=['voice']` becomes `enrich='voice'` on cmd_ask."""
    from unread.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="-1001234567890",
        chat_kind="private",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="last7",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=False,
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=False,
        enrich_kinds=["voice"],
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="open Qs?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["enrich"] == "voice"
    assert kwargs["enrich_all"] is False
    assert kwargs["no_enrich"] is False


@pytest.mark.asyncio
async def test_run_interactive_ask_empty_enrich_kinds_sends_no_enrich():
    """Wizard `enrich_kinds=[]` (user disabled all) → `no_enrich=True`."""
    from unread.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="-1001234567890",
        chat_kind="private",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="last7",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=False,
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=False,
        enrich_kinds=[],
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="open Qs?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["enrich"] is None
    assert kwargs["no_enrich"] is True


@pytest.mark.asyncio
async def test_run_interactive_ask_none_enrich_kinds_uses_defaults():
    """Wizard `enrich_kinds=None` → no override, cmd_ask uses config defaults."""
    from unread.interactive import InteractiveAnswers, run_interactive_ask

    answers = InteractiveAnswers(
        chat_ref="",
        chat_kind="",
        thread_id=None,
        forum_all_flat=False,
        forum_all_per_topic=False,
        preset=None,
        period="unread",
        custom_since=None,
        custom_until=None,
        console_out=False,
        mark_read=False,
        output_path=None,
        run_on_all_unread=False,
        run_on_all_local=True,
        enrich_kinds=None,
        custom_from_msg=None,
        with_comments=False,
    )

    with (
        patch("unread.interactive._collect_answers", new=AsyncMock(return_value=answers)),
        patch("unread.ask.commands.cmd_ask", new=AsyncMock()) as fake_cmd,
    ):
        await run_interactive_ask(question="что нового?")

    kwargs = fake_cmd.call_args.kwargs
    assert kwargs["enrich"] is None
    assert kwargs["enrich_all"] is False
    assert kwargs["no_enrich"] is False


def asyncmgr_factory(value):
    """Decorator: turn a plain class into an async context manager.

    Returning `value` from `__aenter__` mirrors how `tg_client(...)` and
    `open_repo(...)` are used in `cmd_ask`. Using `MagicMock` here forces
    test setup to declare exactly what the production code reads from
    these objects.
    """

    def _wrap(cls):
        async def _aenter(self):
            return value

        async def _aexit(self, *exc):
            return False

        cls.__aenter__ = _aenter
        cls.__aexit__ = _aexit
        return cls

    return _wrap
