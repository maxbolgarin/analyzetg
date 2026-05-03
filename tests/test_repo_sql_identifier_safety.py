"""Tests for the SQL identifier / definition allowlists in `db/repo.py`.

The repo splices table and column names into PRAGMA / DDL statements
because SQL parameter binding doesn't work for identifiers. The
``_assert_safe_*`` helpers are an internal invariant: any future
refactor that lets attacker-controlled strings reach those splices
must fail loudly here, not silently inject DDL.
"""

from __future__ import annotations

import pytest

from unread.db.repo import _assert_safe_column_definition, _assert_safe_sql_name


@pytest.mark.parametrize(
    "name",
    [
        "messages",
        "chats",
        "subscriptions",
        "_meta",
        "transcript_model",
        "T1",
    ],
)
def test_safe_sql_name_accepts_real_identifiers(name: str) -> None:
    _assert_safe_sql_name(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "1bad",  # leading digit
        "messages; DROP TABLE chats",
        "messages--",
        "messages WHERE 1=1",
        "messages\nDROP TABLE chats",
        "messages'",
        '"messages"',  # quoted identifiers not on the allowlist
        "schema.messages",  # dot rejected — call sites pass bare table names
    ],
)
def test_safe_sql_name_rejects_unsafe(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        _assert_safe_sql_name(name)


@pytest.mark.parametrize(
    "definition",
    [
        "INTEGER",
        "TEXT",
        "INTEGER DEFAULT 1",
        "INTEGER NOT NULL DEFAULT 0",
        "TEXT DEFAULT 'summary'",
        "REAL",
        "TIMESTAMP",
        "TEXT NOT NULL",
    ],
)
def test_safe_definition_accepts_real_definitions(definition: str) -> None:
    _assert_safe_column_definition(definition)  # must not raise


@pytest.mark.parametrize(
    "definition",
    [
        "",
        "INTEGER; DROP TABLE chats",
        "INTEGER\nDROP TABLE chats",
        'TEXT DEFAULT "); DROP TABLE chats; --',
        "INTEGER DEFAULT 0 -- comment",
        "TEXT/*comment*/",
    ],
)
def test_safe_definition_rejects_unsafe(definition: str) -> None:
    with pytest.raises(ValueError, match="unsafe SQL column definition"):
        _assert_safe_column_definition(definition)
