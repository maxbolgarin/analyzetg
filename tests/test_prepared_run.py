"""Tests for analyzetg.core.run.PreparedRun.

These pin the dataclass's shape so a consumer (analyze / dump /
download-media) never wakes up to find a field it depended on removed.
"""

from __future__ import annotations

from dataclasses import fields


def test_prepared_run_carries_all_consumer_contracts():
    # Every field a consumer might read. Adding a new field is fine —
    # removing or renaming one needs both this test and the consumers
    # to change in the same commit.
    from analyzetg.core.run import PreparedRun

    expected = {
        "chat_id",
        "thread_id",
        "chat_title",
        "thread_title",
        "chat_username",
        "chat_internal_id",
        "messages",
        "period",
        "topic_titles",
        "topic_markers",
        "raw_msg_count",
        "enrich_stats",
        "mark_read_fn",
        "client",
        "repo",
        "settings",
    }
    actual = {f.name for f in fields(PreparedRun)}
    assert actual == expected, f"missing {expected - actual}, extra {actual - expected}"


def test_prepared_run_slots_enforced():
    # Accidentally adding an attribute outside the declared set should
    # fail fast — slotted dataclass is how we get that guarantee.
    from analyzetg.core.run import PreparedRun

    p = PreparedRun(
        chat_id=1,
        thread_id=None,
        chat_title="t",
        thread_title=None,
        chat_username=None,
        chat_internal_id=None,
        messages=[],
        period=(None, None),
        topic_titles=None,
        topic_markers=None,
        raw_msg_count=0,
        enrich_stats=None,
        mark_read_fn=None,
        client=None,
        repo=None,
        settings=None,
    )
    assert p.messages == []
    try:
        p.some_new_attribute = "nope"  # type: ignore[attr-defined]
    except AttributeError:
        return
    raise AssertionError("PreparedRun should be slotted (no dynamic attrs)")
