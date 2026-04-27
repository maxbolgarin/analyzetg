"""Tests for unread.tg.resolver.rank_candidates (pure helper)."""

from __future__ import annotations

from unread.tg.resolver import rank_candidates


def test_ranker_prefers_exact_title() -> None:
    items = [
        (1, "Bull Trading VIP", "bulltradingvip", "supergroup"),
        (2, "Random Discussion", "randomdisc", "group"),
        (3, "My Notes", None, "user"),
    ]
    ranked = rank_candidates("Bull Trading VIP", items)
    assert ranked[0].chat_id == 1
    assert ranked[0].score >= ranked[1].score


def test_ranker_prefers_username_match() -> None:
    items = [
        (1, "Totally unrelated", "bulltradingvip", "supergroup"),
        (2, "Bull Trading VIP", None, "supergroup"),
    ]
    ranked = rank_candidates("bulltradingvip", items)
    assert ranked[0].chat_id == 1


def test_ranker_handles_empty_pool() -> None:
    assert rank_candidates("anything", []) == []
