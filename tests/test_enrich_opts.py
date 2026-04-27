"""Tests for build_enrich_opts merging CLI → preset → config defaults."""

from __future__ import annotations

import pytest

from atg.analyzer.commands import build_enrich_opts
from atg.analyzer.prompts import Preset
from atg.config import get_settings, reset_settings
from atg.enrich.base import EnrichOpts


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch, tmp_path):
    """Force a clean settings reload for each test — config singleton bites."""
    reset_settings()
    monkeypatch.chdir(tmp_path)
    yield
    reset_settings()


def _preset(enrich_kinds: list[str] | None = None) -> Preset:
    return Preset(
        name="test",
        prompt_version="v1",
        system="sys",
        user_template="{period} {title} {msg_count} {messages}",
        enrich_kinds=list(enrich_kinds or []),
    )


def test_no_enrich_wins_over_all():
    opts = build_enrich_opts(cli_enrich=None, cli_enrich_all=True, cli_no_enrich=True, preset=_preset())
    assert not opts.any_enabled()


def test_enrich_all_sets_everything():
    opts = build_enrich_opts(cli_enrich=None, cli_enrich_all=True, cli_no_enrich=False, preset=_preset())
    assert opts.voice and opts.videonote and opts.video and opts.image and opts.doc and opts.link


def test_enrich_csv_overrides_config():
    # Config default voice=True; user asks for only `image` → voice should go off
    # BUT preset might still union. Here preset is empty, so only image.
    opts = build_enrich_opts(cli_enrich="image", cli_enrich_all=False, cli_no_enrich=False, preset=_preset())
    assert opts.image
    assert not opts.voice
    assert not opts.videonote


def test_enrich_csv_unions_with_preset():
    opts = build_enrich_opts(
        cli_enrich="voice",
        cli_enrich_all=False,
        cli_no_enrich=False,
        preset=_preset(enrich_kinds=["link"]),
    )
    assert opts.voice
    assert opts.link


def test_default_uses_config_plus_preset():
    # No CLI flags, default config: voice=true, videonote=true, link=true,
    # video/image/doc=false. Preset asking for 'link' is a no-op on top
    # of the default since link is already on.
    opts = build_enrich_opts(
        cli_enrich=None,
        cli_enrich_all=False,
        cli_no_enrich=False,
        preset=_preset(enrich_kinds=["link"]),
    )
    assert opts.voice and opts.videonote and opts.link
    assert not opts.image and not opts.doc and not opts.video


def test_default_link_is_disabled_without_preset_request():
    # Link enrichment is opt-in: each unique URL costs an OpenAI call, and
    # users on link-heavy chats were surprised by tens of small requests.
    # A preset that doesn't mention link gets no link enrichment unless the
    # user passes --enrich=link or sets `link = true` in config.toml.
    opts = build_enrich_opts(
        cli_enrich=None,
        cli_enrich_all=False,
        cli_no_enrich=False,
        preset=_preset(),  # empty enrich_kinds
    )
    assert not opts.link, "link was set to default-off; don't quietly flip back"


def test_enrich_csv_rejects_unknown():
    import typer

    with pytest.raises(typer.BadParameter):
        build_enrich_opts(
            cli_enrich="nonsense",
            cli_enrich_all=False,
            cli_no_enrich=False,
            preset=_preset(),
        )


def test_options_carries_caps_from_config():
    # Even with everything off, caps and model choices must come from config.
    s = get_settings()
    opts = build_enrich_opts(cli_enrich=None, cli_enrich_all=False, cli_no_enrich=True, preset=_preset())
    assert opts.max_images_per_run == s.enrich.max_images_per_run
    assert opts.vision_model == s.enrich.vision_model


def test_enrich_opts_any_enabled_false_by_default():
    # EnrichOpts default is all false (per-field defaults).
    assert not EnrichOpts().any_enabled()
