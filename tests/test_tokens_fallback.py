"""tiktoken fallback when the tokenizer blob can't be downloaded.

Pre-prod CI failure: GitHub runners (and developer sandboxes behind
strict egress firewalls) sometimes can't reach
`openaipublic.blob.core.windows.net`. Without a fallback, every
`count_tokens()` call raised `requests.HTTPError` and the entire CLI
became unusable (chunker, cost estimator, dump, ask all use it).

These tests pin the fallback contract:
  * `count_tokens` returns a sensible heuristic when tiktoken raises.
  * `count_message_tokens` survives the same.
  * The warning fires once per process (not per call).
"""

from __future__ import annotations

import unread.util.tokens as tokens_mod
from unread.util.tokens import count_message_tokens, count_tokens


def _kill_tiktoken(monkeypatch):
    """Force tiktoken loads to fail so we exercise the fallback path."""

    class _BoomTiktoken:
        def encoding_for_model(self, model):
            raise RuntimeError("network blocked (test)")

        def get_encoding(self, name):
            raise RuntimeError("network blocked (test)")

    monkeypatch.setattr(tokens_mod, "tiktoken", _BoomTiktoken())
    # Reset the lru_cache so the bad-import wins.
    tokens_mod._encoding_for.cache_clear()
    monkeypatch.setattr(tokens_mod, "_FALLBACK_WARNED", False)


def test_count_tokens_falls_back_when_tiktoken_blocked(monkeypatch):
    _kill_tiktoken(monkeypatch)
    n = count_tokens("hello world how are you", model="gpt-4o-mini")
    assert n > 0
    # Heuristic is char/3 — for 23 chars that's ~7-8 tokens.
    assert 1 <= n <= 30


def test_count_tokens_handles_empty_string_fast_path(monkeypatch):
    _kill_tiktoken(monkeypatch)
    assert count_tokens("") == 0


def test_count_message_tokens_falls_back_safely(monkeypatch):
    _kill_tiktoken(monkeypatch)
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]
    n = count_message_tokens(msgs)
    # Sum of role + content estimates + per-message overhead. Don't
    # pin an exact number — the heuristic is intentionally rough.
    assert n > 0


def test_warning_fires_once_per_process(monkeypatch, capsys):
    _kill_tiktoken(monkeypatch)
    count_tokens("first call", model="m")
    first = capsys.readouterr().out
    count_tokens("second call", model="m")
    second = capsys.readouterr().out
    # Warning emits via structlog's PrintLogger to stdout. Should
    # appear in `first` only.
    assert "tokens.tiktoken_unavailable" in first
    assert "tokens.tiktoken_unavailable" not in second
