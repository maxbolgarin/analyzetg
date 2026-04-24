"""Shared base system prompt + mode-aware forum addendum.

Presets used to each restate "use citation format X", "reactions mean Y",
etc. in their own system prompt, which made any global rule tweak an
N-place edit with N-place cache invalidations. These tests pin the
single-source-of-truth contract:

- `compose_system_prompt` prepends `BASE_SYSTEM` to every preset.
- The forum addendum appears iff `topic_titles` is non-empty — flat-forum
  mode only — so non-forum runs don't spend tokens on irrelevant context.
- `BASE_VERSION` is a module-level constant used as a cache-bust knob.
"""

from __future__ import annotations

from analyzetg.analyzer.prompts import (
    BASE_SYSTEM,
    BASE_VERSION,
    compose_system_prompt,
)


def test_base_system_loaded_from_markdown():
    # _base.md was loaded and is non-trivial (should cover citations,
    # reactions, anti-fabrication). If the file is missing the loader
    # falls back to a one-line string — catch that regression here.
    assert len(BASE_SYSTEM) > 200
    # Key guarantees the base is supposed to encode:
    assert "msg_id" in BASE_SYSTEM
    assert "реакции" in BASE_SYSTEM.lower() or "reactions" in BASE_SYSTEM.lower()


def test_compose_without_topics_is_base_plus_preset():
    composed = compose_system_prompt("PRESET-TASK-TEXT")
    assert composed.startswith(BASE_SYSTEM)
    assert composed.endswith("PRESET-TASK-TEXT")
    # No forum-specific language when topic_titles is absent.
    assert "=== Топик:" not in composed
    assert "Форум-режим" not in composed


def test_compose_with_topics_inserts_forum_addendum():
    composed = compose_system_prompt("TASK", topic_titles={1: "A", 2: "B"})
    assert BASE_SYSTEM in composed
    assert "TASK" in composed
    # The addendum references the topic-group header format the
    # formatter actually emits, so the LLM's expectations and the
    # data it sees stay aligned.
    assert "=== Топик:" in composed
    # Must be in the right slot: AFTER base, BEFORE preset task.
    base_end = composed.index(BASE_SYSTEM) + len(BASE_SYSTEM)
    task_start = composed.index("TASK")
    addendum_start = composed.index("Форум-режим")
    assert base_end <= addendum_start < task_start


def test_compose_with_empty_topics_behaves_like_none():
    # Sending an empty dict through the pipeline (fresh forum with no
    # topics fetched yet) must not trigger the addendum — it would be a
    # pure waste of context and lie about the chat structure.
    assert compose_system_prompt("X", topic_titles={}) == compose_system_prompt("X")


def test_base_version_is_string_constant():
    # Gets threaded through options_payload in analyzer/pipeline.py; any
    # base-prompt change should bump this to bust cached results.
    assert isinstance(BASE_VERSION, str) and BASE_VERSION


def test_forum_addendum_prescribes_structural_rules():
    # Regression guard for the real issue a user hit: flat-forum summary
    # produced a 20-bullet `## Главное` with no topic separation at all.
    # The addendum is the one place we can fix this for all presets at
    # once — so pin that it actually tells the LLM to use per-topic
    # subsections and to add a TL;DR for the occasional reader.
    composed = compose_system_prompt("irrelevant", topic_titles={1: "A", 2: "B"})
    # Prescribes ### subsections keyed by topic name, not a flat list.
    assert "###" in composed
    assert "по топикам" in composed.lower() or "per topic" in composed.lower()
    # Prescribes a TL;DR line so skimmers have an anchor.
    assert "TL;DR" in composed or "tl;dr" in composed.lower()
