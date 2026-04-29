"""`Repo._parse_schema_columns` ignores comments containing `,` and `;`.

Regression: a previous version of the parser stripped `--`-comments
*after* the comma split, so a column like
``transcript_timed_json TEXT, -- JSON [[start_sec, "text"], …];``
yielded phantom "columns" `text"]` and `…];` — surfacing a noisy
schema-drift WARN on every doctor run.
"""

from __future__ import annotations

from unread.db.repo import Repo


def test_comment_with_comma_does_not_create_phantom_columns():
    sql = """
    CREATE TABLE IF NOT EXISTS x (
        a INTEGER PRIMARY KEY,
        timed TEXT,        -- JSON [[start_sec, "text"], …]; for captions
        b TEXT
    );
    """
    cols = Repo._parse_schema_columns(sql)
    assert cols == {"x": {"a", "timed", "b"}}


def test_constraint_clauses_are_filtered():
    sql = """
    CREATE TABLE IF NOT EXISTS y (
        id INTEGER,
        name TEXT,
        PRIMARY KEY (id, name),
        UNIQUE (name)
    );
    """
    cols = Repo._parse_schema_columns(sql)
    assert cols == {"y": {"id", "name"}}
