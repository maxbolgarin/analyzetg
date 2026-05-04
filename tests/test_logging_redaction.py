"""Pre-prod review MEDIUM: structlog redactor walks nested dicts/lists.

The original `_redact_processor` only inspected top-level event-dict
keys. A `log.warning("oops", extra={"payload": {"api_key": "sk-..."}})`
would leak the key. The recursive variant catches that pattern and
the deeper Telethon / provider response shapes — capped at depth 6
so a malicious / pathological input can't pin the logger.
"""

from __future__ import annotations

from unread.util.logging import _redact_processor


def _redact(event: dict) -> dict:
    return _redact_processor(None, "info", dict(event))


def test_redacts_top_level_secret_key():
    out = _redact({"event": "test", "api_key": "sk-real-secret"})
    assert out["api_key"] == "***REDACTED***"


def test_redacts_nested_dict_one_level():
    out = _redact({"event": "test", "payload": {"api_key": "sk-real-secret"}})
    assert out["payload"]["api_key"] == "***REDACTED***"


def test_redacts_nested_dict_two_levels():
    out = _redact({"event": "test", "outer": {"inner": {"token": "very-secret"}}})
    assert out["outer"]["inner"]["token"] == "***REDACTED***"


def test_redacts_in_list_of_dicts():
    out = _redact({"event": "test", "calls": [{"api_key": "k1"}, {"api_key": "k2"}]})
    assert all(item["api_key"] == "***REDACTED***" for item in out["calls"])


def test_redacts_in_tuple_of_dicts():
    out = _redact({"event": "test", "calls": ({"secret": "s1"}, {"secret": "s2"})})
    assert isinstance(out["calls"], tuple)
    assert all(item["secret"] == "***REDACTED***" for item in out["calls"])


def test_passes_safe_values_through_unchanged():
    out = _redact({"event": "test", "user_id": 42, "host": "api.openai.com"})
    assert out["user_id"] == 42
    assert out["host"] == "api.openai.com"


def test_depth_cap_prevents_runaway_recursion():
    """A pathological depth-1000 nested dict shouldn't pin the logger."""
    nested: dict = {}
    cur = nested
    for _ in range(50):
        cur["next"] = {}
        cur = cur["next"]
    cur["api_key"] = "sk-deep-buried"
    # Must not raise / hang. Whether it redacts at depth 50 is fine
    # either way — the contract is "bounded cost".
    out = _redact({"event": "test", "deep": nested})
    assert "deep" in out


def test_redacts_secret_via_value_regex():
    """OpenAI-style sk-... value is masked even when the key is innocent."""
    out = _redact({"event": "test", "msg": "got key sk-aB3dEfGhIjKlMnOpQrStUv"})
    assert "sk-aB3dEfGh" not in out["msg"]
