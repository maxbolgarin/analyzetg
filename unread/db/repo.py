"""Async SQLite repository.

Thin wrapper around aiosqlite with typed methods per spec §4. `schema.sql`
is the single source of truth and is applied (idempotently) on every
connect. Additive compatibility checks run before the schema script so
older local DB files can receive columns that `CREATE TABLE IF NOT EXISTS`
would otherwise skip.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from unread.models import Message, Subscription, SyncState
from unread.util.logging import get_logger

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Bumped whenever the schema gains additive changes the older code
# can't tolerate (column with NOT NULL / no default, dropped column,
# semantic table rewrite). Pure additive nullable columns DON'T need
# a bump — older code just ignores them. The stamp lives in
# `app_settings::_meta.schema_version` so a downgrade can detect a
# future-version DB and refuse cleanly instead of crashing on a
# missing column.
SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    """Raised when the on-disk DB was written by a future, incompatible version."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _from_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    return datetime.fromisoformat(val)


class Repo:
    """Async repository. Construct with `await Repo.open(path)`."""

    def __init__(self, conn: aiosqlite.Connection, path: Path) -> None:
        self._conn = conn
        self._path = path

    @classmethod
    async def open(cls, path: Path | str) -> Repo:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(p)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")
        repo = cls(conn, p)
        try:
            await repo._apply_schema()
        except BaseException:
            # Don't leave the aiosqlite worker thread dangling if schema
            # application bails out. Without this, the test harness
            # complains about "Event loop is closed" when the thread tries
            # to signal a future after the loop ends.
            await conn.close()
            raise
        # aiosqlite (via the stdlib sqlite3 connector) creates the file
        # with the process umask — usually 0o644, world-readable. The DB
        # holds cached chat content, enrichment results, secrets, and
        # cost logs. Tighten to owner-only on every open (idempotent).
        # Also tighten the WAL/SHM siblings if they exist.
        from unread.util.fsmode import tighten

        tighten(p)
        for sibling in (p.with_suffix(p.suffix + "-wal"), p.with_suffix(p.suffix + "-shm")):
            if sibling.exists():
                tighten(sibling)
        # Compare on-disk schema version with the code's expected version.
        # If the DB was written by a newer version that introduced a
        # change the current code can't read, refuse to use it instead
        # of crashing later on a column-not-found.
        try:
            await repo._check_and_stamp_schema_version()
        except SchemaVersionError:
            await conn.close()
            raise
        return repo

    async def close(self) -> None:
        await self._conn.close()

    async def _check_and_stamp_schema_version(self) -> None:
        """Read on-disk schema version, refuse if too new, otherwise stamp.

        First-time DBs start at the current `SCHEMA_VERSION`. An older
        DB whose stamp is missing or numeric-less than ours is silently
        upgraded to ours (the additive-migration pass already ran).
        A DB with a stamp greater than ours (a previously-installed
        newer release wrote it) raises :class:`SchemaVersionError`
        with copy that tells the user to upgrade.
        """
        cur = await self._conn.execute(
            "SELECT value FROM app_settings WHERE key=?",
            ("_meta.schema_version",),
        )
        row = await cur.fetchone()
        await cur.close()
        on_disk: int | None = None
        if row is not None:
            try:
                on_disk = int(row["value"])
            except (TypeError, ValueError):
                on_disk = None
        if on_disk is not None and on_disk > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Your storage DB at {self._path} was written by a newer "
                f"unread (schema v{on_disk}); this build understands up to "
                f"v{SCHEMA_VERSION}. Upgrade with `pip install -U unread`, "
                f"or remove the DB to start fresh: rm {self._path}"
            )
        if on_disk != SCHEMA_VERSION:
            await self._conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                ("_meta.schema_version", str(SCHEMA_VERSION), _utcnow()),
            )
            await self._conn.commit()

    async def _apply_schema(self) -> None:
        """Idempotently apply `schema.sql`.

        `CREATE TABLE IF NOT EXISTS` handles fresh DBs, but does not add
        columns to existing tables. Run a tiny additive compatibility pass
        first so indexes and repo methods can rely on the current columns.
        After applying, run a drift check that warns when an existing
        table is missing columns declared in schema.sql but not listed
        in `_apply_additive_migrations` — that combination silently
        breaks queries, and the warning gives the maintainer the exact
        ALTER they need to add.
        """
        await self._apply_additive_migrations()
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        await self._conn.executescript(sql)
        await self._conn.commit()
        await self._warn_on_schema_drift(sql)

    async def _table_columns(self, table: str) -> set[str]:
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        await cur.close()
        return {str(row["name"]) for row in rows}

    async def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = await self._table_columns(table)
        if not existing:
            return
        for name, definition in columns.items():
            if name not in existing:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    async def _sqlite_object_type(self, name: str) -> str | None:
        cur = await self._conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,))
        row = await cur.fetchone()
        await cur.close()
        return str(row["type"]) if row else None

    async def _migrate_legacy_media_transcripts(self) -> None:
        kind = await self._sqlite_object_type("media_transcripts")
        if kind == "view":
            await self._conn.execute("DROP VIEW IF EXISTS media_transcripts")
            return
        if kind != "table":
            return
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_enrichments (
                doc_id       INTEGER NOT NULL,
                kind         TEXT NOT NULL,
                content      TEXT NOT NULL,
                model        TEXT,
                cost_usd     REAL,
                duration_sec INTEGER,
                language     TEXT,
                file_sha1    TEXT,
                extra_json   TEXT,
                created_at   TIMESTAMP,
                PRIMARY KEY (doc_id, kind)
            )
            """
        )
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO media_enrichments(
                doc_id, kind, content, model, cost_usd, duration_sec,
                language, file_sha1, extra_json, created_at
            )
            SELECT doc_id, 'transcript', transcript, model, cost_usd,
                   duration_sec, language, file_sha1, NULL, created_at
            FROM media_transcripts
            """
        )
        await self._conn.execute("DROP TABLE media_transcripts")

    async def _apply_additive_migrations(self) -> None:
        """Apply forward-compatible column additions for pre-schema.sql DBs."""
        await self._add_missing_columns("chats", {"linked_chat_id": "INTEGER"})
        await self._add_missing_columns(
            "subscriptions",
            {
                "transcribe_voice": "INTEGER DEFAULT 1",
                "transcribe_videonote": "INTEGER DEFAULT 1",
                "transcribe_video": "INTEGER DEFAULT 0",
                "preset": "TEXT DEFAULT 'summary'",
                "period": "TEXT DEFAULT 'unread'",
                "enrich_kinds": "TEXT",
                "mark_read": "INTEGER DEFAULT 1",
                "post_to": "TEXT",
            },
        )
        await self._add_missing_columns(
            "messages",
            {
                "media_type": "TEXT",
                "media_doc_id": "INTEGER",
                "media_duration": "INTEGER",
                "transcript": "TEXT",
                "transcript_model": "TEXT",
                "reactions": "TEXT",
            },
        )
        await self._add_missing_columns("analysis_cache", {"truncated": "INTEGER NOT NULL DEFAULT 0"})
        await self._add_missing_columns("usage_log", {"cached_tokens": "INTEGER"})
        await self._add_missing_columns(
            "youtube_videos",
            {"transcript_timed_json": "TEXT"},
        )
        await self._migrate_legacy_media_transcripts()
        await self._conn.commit()

    # `CREATE TABLE foo (...);` block — captures table name and the
    # column list. We tolerate `IF NOT EXISTS` and surrounding whitespace.
    # This regex is intentionally simple: schema.sql is a hand-written
    # file we control, not arbitrary user SQL, so we don't need to
    # handle every CREATE-statement variant SQLite accepts.
    _CREATE_TABLE_RE = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )

    @staticmethod
    def _parse_schema_columns(sql: str) -> dict[str, set[str]]:
        """Extract `{table_name: {column_names…}}` from `schema.sql` text.

        Skips `PRIMARY KEY (...)` and other constraint clauses by only
        treating top-level entries whose first whitespace-split token
        looks like a column name (alphanumeric + underscore, not a
        SQL keyword).
        """
        # Reserved tokens that show up as the first word of constraint
        # clauses inside `CREATE TABLE (...)`. Anything else is treated
        # as a column declaration.
        constraint_tokens = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}
        out: dict[str, set[str]] = {}
        for m in Repo._CREATE_TABLE_RE.finditer(sql):
            table = m.group(1)
            # Strip line comments BEFORE the comma split — comments can
            # contain commas (e.g. `-- JSON [[a, "b"], …]`) which would
            # otherwise be treated as column separators and yield
            # phantom "columns" like `"text"]` or `…];`.
            body = re.sub(r"--[^\n]*", "", m.group(2))
            cols: set[str] = set()
            depth = 0
            buf: list[str] = []
            entries: list[str] = []
            # Split on top-level commas only — `column REAL DEFAULT (a, b)`
            # would otherwise blow up. depth tracks nested parentheses.
            for ch in body:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if ch == "," and depth == 0:
                    entries.append("".join(buf))
                    buf = []
                else:
                    buf.append(ch)
            if buf:
                entries.append("".join(buf))
            for entry in entries:
                cleaned = entry.strip()
                if not cleaned:
                    continue
                first = cleaned.split(None, 1)[0].strip('"')
                if first.upper() in constraint_tokens:
                    continue
                cols.add(first)
            if cols:
                out[table] = cols
        return out

    async def _warn_on_schema_drift(self, schema_sql: str) -> None:
        """Compare runtime PRAGMA columns against schema.sql declarations.

        Catches the case where `schema.sql` declares a new column on an
        existing table but the maintainer forgot to add the matching
        `ALTER TABLE` to ``_apply_additive_migrations``. SQLite's
        ``CREATE TABLE IF NOT EXISTS`` does NOT add columns to a table
        that already exists, so the new column never materializes and
        any query referencing it silently breaks.

        We only WARN (not auto-ALTER): the project deliberately ships
        without a migration runner, and a destructive-by-default fixup
        would surprise users. The warning carries the exact ALTER the
        maintainer needs to add.
        """
        declared = self._parse_schema_columns(schema_sql)
        for table, expected_cols in declared.items():
            try:
                actual = await self._table_columns(table)
            except Exception:
                # Table introspection failed (locked DB, etc) — drift
                # detection is best-effort, never blocks startup.
                continue
            if not actual:
                # Table not yet created — that's normal on a fresh DB
                # before `executescript` runs the CREATE statements. By
                # the time we're called, executescript has already run,
                # so an empty PRAGMA likely means a virtual / FTS table
                # that PRAGMA can't introspect cleanly. Skip.
                continue
            missing = expected_cols - actual
            if missing:
                log.warning(
                    "db.schema_drift",
                    table=table,
                    missing=sorted(missing),
                    hint=(
                        f"schema.sql declares column(s) {sorted(missing)} on "
                        f"`{table}` but the existing table is missing them. "
                        "Add a matching `_add_missing_columns(...)` line to "
                        "`Repo._apply_additive_migrations` so existing DBs "
                        "pick up the change."
                    ),
                )

    # ------------------------------------------------------------------ chats

    async def upsert_chat(
        self,
        chat_id: int,
        kind: str,
        title: str | None = None,
        username: str | None = None,
        linked_chat_id: int | None = None,
    ) -> None:
        now = _utcnow()
        await self._conn.execute(
            """
            INSERT INTO chats(id, kind, title, username, linked_chat_id, first_seen_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,
                title=COALESCE(excluded.title, chats.title),
                username=COALESCE(excluded.username, chats.username),
                linked_chat_id=COALESCE(excluded.linked_chat_id, chats.linked_chat_id),
                updated_at=excluded.updated_at
            """,
            (chat_id, kind, title, username, linked_chat_id, now, now),
        )
        await self._conn.commit()

    async def get_chat(self, chat_id: int) -> dict[str, Any] | None:
        cur = await self._conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def find_chat_by_username(self, username: str) -> dict[str, Any] | None:
        cur = await self._conn.execute("SELECT * FROM chats WHERE username = ? COLLATE NOCASE", (username,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def search_chats_by_title(self, fragment: str, limit: int = 50) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM chats WHERE title LIKE ? COLLATE NOCASE LIMIT ?",
            (f"%{fragment}%", limit),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- subscriptions

    async def upsert_subscription(self, sub: Subscription) -> None:
        await self._conn.execute(
            """
            INSERT INTO subscriptions(chat_id, thread_id, title, source_kind, enabled,
                start_from_msg_id, start_from_date, transcribe_voice, transcribe_videonote,
                transcribe_video, preset, period, enrich_kinds, mark_read, post_to,
                added_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                title=excluded.title,
                source_kind=excluded.source_kind,
                enabled=excluded.enabled,
                start_from_msg_id=COALESCE(excluded.start_from_msg_id, subscriptions.start_from_msg_id),
                start_from_date=COALESCE(excluded.start_from_date, subscriptions.start_from_date),
                transcribe_voice=excluded.transcribe_voice,
                transcribe_videonote=excluded.transcribe_videonote,
                transcribe_video=excluded.transcribe_video,
                preset=excluded.preset,
                period=excluded.period,
                enrich_kinds=excluded.enrich_kinds,
                mark_read=excluded.mark_read,
                post_to=excluded.post_to
            """,
            (
                sub.chat_id,
                sub.thread_id,
                sub.title,
                sub.source_kind,
                int(sub.enabled),
                sub.start_from_msg_id,
                sub.start_from_date.isoformat() if sub.start_from_date else None,
                int(sub.transcribe_voice),
                int(sub.transcribe_videonote),
                int(sub.transcribe_video),
                sub.preset or "summary",
                sub.period or "unread",
                sub.enrich_kinds,
                int(sub.mark_read),
                sub.post_to,
                (sub.added_at or datetime.now(UTC)).isoformat(),
            ),
        )
        await self._conn.commit()

    async def list_subscriptions(self, enabled_only: bool = False) -> list[Subscription]:
        sql = "SELECT * FROM subscriptions"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY chat_id, thread_id"
        cur = await self._conn.execute(sql)
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_sub(r) for r in rows]

    async def get_subscription(self, chat_id: int, thread_id: int = 0) -> Subscription | None:
        cur = await self._conn.execute(
            "SELECT * FROM subscriptions WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        row = await cur.fetchone()
        await cur.close()
        return self._row_to_sub(row) if row else None

    async def set_subscription_enabled(self, chat_id: int, thread_id: int, enabled: bool) -> None:
        await self._conn.execute(
            "UPDATE subscriptions SET enabled=? WHERE chat_id=? AND thread_id=?",
            (int(enabled), chat_id, thread_id),
        )
        await self._conn.commit()

    async def remove_subscription(self, chat_id: int, thread_id: int, purge_messages: bool = False) -> None:
        await self._conn.execute(
            "DELETE FROM subscriptions WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        await self._conn.execute(
            "DELETE FROM sync_state WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        if purge_messages:
            if thread_id == 0:
                await self._conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
            else:
                await self._conn.execute(
                    "DELETE FROM messages WHERE chat_id=? AND thread_id=?",
                    (chat_id, thread_id),
                )
        await self._conn.commit()

    @staticmethod
    def _row_to_sub(row: aiosqlite.Row) -> Subscription:
        keys = row.keys()
        return Subscription(
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            title=row["title"],
            source_kind=row["source_kind"],
            enabled=bool(row["enabled"]),
            start_from_msg_id=row["start_from_msg_id"],
            start_from_date=_from_ts(row["start_from_date"]),
            transcribe_voice=bool(row["transcribe_voice"]),
            transcribe_videonote=bool(row["transcribe_videonote"]),
            transcribe_video=bool(row["transcribe_video"]),
            preset=(row["preset"] if "preset" in keys and row["preset"] is not None else "summary"),
            period=(row["period"] if "period" in keys and row["period"] is not None else "unread"),
            enrich_kinds=(row["enrich_kinds"] if "enrich_kinds" in keys else None),
            mark_read=(
                bool(row["mark_read"]) if "mark_read" in keys and row["mark_read"] is not None else True
            ),
            post_to=(row["post_to"] if "post_to" in keys else None),
            added_at=_from_ts(row["added_at"]),
        )

    # --------------------------------------------------------------- messages

    async def upsert_messages(self, msgs: Iterable[Message]) -> int:
        rows = [self._msg_to_row(m) for m in msgs]
        if not rows:
            return 0
        await self._conn.executemany(
            """
            INSERT INTO messages(chat_id, msg_id, thread_id, date, sender_id, sender_name,
                text, reply_to, forward_from, media_type, media_doc_id, media_duration,
                reactions)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, msg_id) DO UPDATE SET
                thread_id=excluded.thread_id,
                date=excluded.date,
                sender_id=excluded.sender_id,
                sender_name=excluded.sender_name,
                text=excluded.text,
                reply_to=excluded.reply_to,
                forward_from=excluded.forward_from,
                media_type=COALESCE(excluded.media_type, messages.media_type),
                media_doc_id=COALESCE(excluded.media_doc_id, messages.media_doc_id),
                media_duration=COALESCE(excluded.media_duration, messages.media_duration),
                reactions=excluded.reactions
            """,
            rows,
        )
        await self._conn.commit()
        return len(rows)

    @staticmethod
    def _msg_to_row(m: Message) -> tuple:
        return (
            m.chat_id,
            m.msg_id,
            m.thread_id,
            m.date.isoformat(),
            m.sender_id,
            m.sender_name,
            m.text,
            m.reply_to,
            m.forward_from,
            m.media_type,
            m.media_doc_id,
            m.media_duration,
            json.dumps(m.reactions, ensure_ascii=False) if m.reactions else None,
        )

    async def iter_messages(
        self,
        chat_id: int,
        thread_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        min_msg_id: int | None = None,
        max_msg_id: int | None = None,
    ) -> list[Message]:
        sql = "SELECT * FROM messages WHERE chat_id=?"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        if since:
            sql += " AND date >= ?"
            args.append(since.isoformat())
        if until:
            sql += " AND date <= ?"
            args.append(until.isoformat())
        if min_msg_id is not None:
            sql += " AND msg_id > ?"
            args.append(min_msg_id)
        if max_msg_id is not None:
            sql += " AND msg_id <= ?"
            args.append(max_msg_id)
        sql += " ORDER BY date ASC, msg_id ASC"
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_msg(r) for r in rows]

    @staticmethod
    def _row_to_msg(row: aiosqlite.Row) -> Message:
        date = _from_ts(row["date"])
        assert date is not None
        reactions_raw = row["reactions"]
        reactions: dict[str, int] | None = None
        if reactions_raw:
            try:
                parsed = json.loads(reactions_raw)
                if isinstance(parsed, dict):
                    reactions = {str(k): int(v) for k, v in parsed.items()}
            except (ValueError, TypeError):
                reactions = None
        return Message(
            chat_id=row["chat_id"],
            msg_id=row["msg_id"],
            date=date,
            thread_id=row["thread_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=row["text"],
            reply_to=row["reply_to"],
            forward_from=row["forward_from"],
            media_type=row["media_type"],
            media_doc_id=row["media_doc_id"],
            media_duration=row["media_duration"],
            transcript=row["transcript"],
            transcript_model=row["transcript_model"],
            reactions=reactions,
        )

    async def get_max_msg_id(
        self,
        chat_id: int,
        thread_id: int | None = None,
        min_msg_id: int | None = None,
    ) -> int | None:
        """Highest msg_id we already have for this chat/thread above `min_msg_id`.

        Used by analyze/dump to skip refetching messages already in the DB.
        Returns None if no rows match.
        """
        sql = "SELECT MAX(msg_id) AS m FROM messages WHERE chat_id=?"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        if min_msg_id is not None:
            sql += " AND msg_id > ?"
            args.append(min_msg_id)
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        if not row or row["m"] is None:
            return None
        return int(row["m"])

    async def get_min_msg_id(
        self,
        chat_id: int,
        thread_id: int | None = None,
    ) -> int | None:
        """Lowest msg_id stored for this chat/thread, or None if empty.

        Used by the full-history backfill path: we walk backward from
        this value to msg_id=1 to pull the pre-sync history we've never
        seen. Without it, "full history" would only ever mean "history
        since the first time we synced".
        """
        sql = "SELECT MIN(msg_id) AS m FROM messages WHERE chat_id=?"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        if not row or row["m"] is None:
            return None
        return int(row["m"])

    async def chat_stats(self, chat_id: int, thread_id: int | None = None) -> dict[str, Any]:
        """Summary stats for a chat (or thread): count and date range."""
        sql = "SELECT COUNT(*) AS c, MIN(date) AS dmin, MAX(date) AS dmax FROM messages WHERE chat_id=?"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return {"count": 0, "date_min": None, "date_max": None}
        return {
            "count": int(row["c"] or 0),
            "date_min": _from_ts(row["dmin"]),
            "date_max": _from_ts(row["dmax"]),
        }

    async def top_senders(
        self, chat_id: int, thread_id: int | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Top-N senders by message count in a chat (or thread)."""
        sql = "SELECT sender_name, COUNT(*) AS c FROM messages WHERE chat_id=? AND sender_name IS NOT NULL"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        sql += " GROUP BY sender_name ORDER BY c DESC LIMIT ?"
        args.append(limit)
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [{"sender_name": r["sender_name"], "count": int(r["c"])} for r in rows]

    async def count_messages(self, chat_id: int, thread_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM messages WHERE chat_id=?"
        args: list[Any] = [chat_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        return int(row["c"]) if row else 0

    async def get_messages_around(
        self,
        chat_id: int,
        msg_id: int,
        *,
        before: int = 3,
        after: int = 3,
        thread_id: int | None = None,
    ) -> list[Message]:
        """Fetch up to N messages on each side of `msg_id` by msg_id ordering.

        Used by `--cite-context` to expand each citation in the analysis
        report into a context block (audit trail). Msg_ids are
        per-chat-sequential, so ordering by id is reliable for "what
        came right before/after". Two range queries — one above, one
        below — joined with the anchor message.
        """
        # Below: msg_id - before .. msg_id - 1
        before_rows: list[Message] = []
        if before > 0:
            sql = "SELECT * FROM messages WHERE chat_id=? AND msg_id < ?"
            args: list[Any] = [chat_id, msg_id]
            if thread_id is not None:
                sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
                args.extend([thread_id, thread_id])
            sql += " ORDER BY msg_id DESC LIMIT ?"
            args.append(before)
            cur = await self._conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
            before_rows = [self._row_to_msg(r) for r in rows]
            before_rows.reverse()  # oldest-first

        # Anchor + after.
        sql = "SELECT * FROM messages WHERE chat_id=? AND msg_id >= ?"
        args = [chat_id, msg_id]
        if thread_id is not None:
            sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
            args.extend([thread_id, thread_id])
        sql += " ORDER BY msg_id ASC LIMIT ?"
        args.append(after + 1)
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        anchor_and_after = [self._row_to_msg(r) for r in rows]
        return before_rows + anchor_and_after

    async def media_breakdown(
        self,
        chat_id: int,
        thread_id: int | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        min_msg_id: int | None = None,
        max_msg_id: int | None = None,
    ) -> dict[str, int]:
        """Return `{media_type: count, "text": N, "links": N, "total": N}`.

        Counts every message in the local DB matching the filters and
        groups by `media_type`. `text` = messages whose `text` is non-empty
        (regardless of media kind). `links` = messages with a likely URL
        (cheap `LIKE '%http%'` proxy). Used by the wizard to show "voice:
        36, image: 5, …" so the user knows enrichment scope before
        committing.

        Counts reflect what's already synced into SQLite — backfill at run
        time may add more.
        """
        where = ["chat_id = ?"]
        args: list[Any] = [chat_id]
        if thread_id is not None:
            where.append("(thread_id = ? OR (? = 0 AND thread_id IS NULL))")
            args.extend([thread_id, thread_id])
        if since:
            where.append("date >= ?")
            args.append(since.isoformat())
        if until:
            where.append("date <= ?")
            args.append(until.isoformat())
        if min_msg_id is not None:
            where.append("msg_id > ?")
            args.append(min_msg_id)
        if max_msg_id is not None:
            where.append("msg_id <= ?")
            args.append(max_msg_id)
        wsql = " AND ".join(where)
        sql = f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN media_type IS NOT NULL THEN 1 ELSE 0 END) AS any_media,
                SUM(CASE WHEN media_type = 'voice' THEN 1 ELSE 0 END) AS voice,
                SUM(CASE WHEN media_type = 'videonote' THEN 1 ELSE 0 END) AS videonote,
                SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END) AS video,
                SUM(CASE WHEN media_type = 'photo' THEN 1 ELSE 0 END) AS photo,
                SUM(CASE WHEN media_type = 'doc' THEN 1 ELSE 0 END) AS doc,
                SUM(CASE WHEN text IS NOT NULL AND text != '' THEN 1 ELSE 0 END) AS text,
                SUM(CASE WHEN text LIKE '%http%' THEN 1 ELSE 0 END) AS links
            FROM messages
            WHERE {wsql}
        """
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return {
                "total": 0,
                "any_media": 0,
                "voice": 0,
                "videonote": 0,
                "video": 0,
                "photo": 0,
                "doc": 0,
                "text": 0,
                "links": 0,
            }
        # aiosqlite/sqlite3 Row iterates values, not keys — explicitly enumerate
        # the columns we asked for so the dict shape stays stable.
        keys = ("total", "any_media", "voice", "videonote", "video", "photo", "doc", "text", "links")
        return {k: int(row[k] or 0) for k in keys}

    async def set_message_transcript(self, chat_id: int, msg_id: int, transcript: str, model: str) -> None:
        await self._conn.execute(
            "UPDATE messages SET transcript=?, transcript_model=? WHERE chat_id=? AND msg_id=?",
            (transcript, model, chat_id, msg_id),
        )
        await self._conn.commit()

    async def untranscribed_media(
        self,
        chat_id: int | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[Message]:
        sql = (
            "SELECT * FROM messages WHERE media_doc_id IS NOT NULL AND transcript IS NULL"
            " AND media_type IS NOT NULL"
        )
        args: list[Any] = []
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        if since:
            sql += " AND date >= ?"
            args.append(since.isoformat())
        if until:
            sql += " AND date <= ?"
            args.append(until.isoformat())
        sql += " ORDER BY date ASC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_msg(r) for r in rows]

    async def count_redactable_messages(
        self,
        retention_days: int,
        chat_id: int | None = None,
        keep_transcripts: bool = True,
    ) -> dict[str, int]:
        """Preview what redact_old_messages would affect. Returns
        {"messages": N, "with_text": N, "with_transcript": N, "to_redact": N}.
        `to_redact` = rows that actually have something to null given `keep_transcripts`."""
        if retention_days <= 0:
            return {"messages": 0, "with_text": 0, "with_transcript": 0, "to_redact": 0}
        sql = (
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN text IS NOT NULL THEN 1 ELSE 0 END) AS with_text,"
            " SUM(CASE WHEN transcript IS NOT NULL THEN 1 ELSE 0 END) AS with_transcript,"
            " SUM(CASE WHEN text IS NOT NULL"
            "          OR (? = 0 AND transcript IS NOT NULL) THEN 1 ELSE 0 END) AS to_redact"
            " FROM messages WHERE datetime(date) < datetime('now', ?)"
        )
        args: list[Any] = [1 if keep_transcripts else 0, f"-{retention_days} days"]
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        return {
            "messages": int(row["n"] or 0),
            "with_text": int(row["with_text"] or 0),
            "with_transcript": int(row["with_transcript"] or 0),
            "to_redact": int(row["to_redact"] or 0),
        }

    async def redact_old_messages(
        self,
        retention_days: int,
        chat_id: int | None = None,
        keep_transcripts: bool = True,
    ) -> int:
        if retention_days <= 0:
            return 0
        sql = "UPDATE messages SET text=NULL"
        if not keep_transcripts:
            sql += ", transcript=NULL"
        # datetime() parses both our stored ISO-T strings and SQLite's space-separated
        # output, so we can compare them directly.
        sql += " WHERE datetime(date) < datetime('now', ?)"
        args: list[Any] = [f"-{retention_days} days"]
        if chat_id is not None:
            sql += " AND chat_id=?"
            args.append(chat_id)
        # Skip rows that have nothing left to null — otherwise rowcount
        # reports matches, not actual redactions.
        if keep_transcripts:
            sql += " AND text IS NOT NULL"
        else:
            sql += " AND (text IS NOT NULL OR transcript IS NOT NULL)"
        cur = await self._conn.execute(sql, args)
        await self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------ sync_state

    async def get_sync_state(self, chat_id: int, thread_id: int = 0) -> SyncState | None:
        cur = await self._conn.execute(
            "SELECT * FROM sync_state WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        return SyncState(
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            last_msg_id=row["last_msg_id"],
            last_synced_at=_from_ts(row["last_synced_at"]),
        )

    async def update_sync_state(self, chat_id: int, thread_id: int, last_msg_id: int) -> None:
        await self._conn.execute(
            """
            INSERT INTO sync_state(chat_id, thread_id, last_msg_id, last_synced_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                last_msg_id=MAX(sync_state.last_msg_id, excluded.last_msg_id),
                last_synced_at=excluded.last_synced_at
            """,
            (chat_id, thread_id, last_msg_id, _utcnow()),
        )
        await self._conn.commit()

    # ---------------------------------------------------------- enrichments

    async def get_media_enrichment(self, doc_id: int, kind: str) -> dict[str, Any] | None:
        """Fetch a cached enrichment row by (doc_id, kind).

        `kind` is one of: transcript, image_description, doc_extract,
        video_description. Returns a dict with the full row or None.
        """
        cur = await self._conn.execute(
            "SELECT * FROM media_enrichments WHERE doc_id=? AND kind=?",
            (doc_id, kind),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_media_enrichment(
        self,
        doc_id: int,
        kind: str,
        content: str,
        *,
        model: str | None = None,
        cost_usd: float | None = None,
        duration_sec: int | None = None,
        language: str | None = None,
        file_sha1: str | None = None,
        extra_json: str | None = None,
    ) -> None:
        """Upsert a content-addressable enrichment. `doc_id` dedups the same
        media across chats — one photo forwarded 10 times = one row.
        """
        await self._conn.execute(
            """
            INSERT INTO media_enrichments(doc_id, kind, content, model, cost_usd,
                duration_sec, language, file_sha1, extra_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id, kind) DO UPDATE SET
                content=excluded.content,
                model=excluded.model,
                cost_usd=excluded.cost_usd,
                duration_sec=COALESCE(excluded.duration_sec, media_enrichments.duration_sec),
                language=COALESCE(excluded.language, media_enrichments.language),
                file_sha1=COALESCE(excluded.file_sha1, media_enrichments.file_sha1),
                extra_json=COALESCE(excluded.extra_json, media_enrichments.extra_json)
            """,
            (
                doc_id,
                kind,
                content,
                model,
                cost_usd,
                duration_sec,
                language,
                file_sha1,
                extra_json,
                _utcnow(),
            ),
        )
        await self._conn.commit()

    # Backward-compat wrappers for the old transcript-only API. New code should
    # call get_media_enrichment/put_media_enrichment with kind='transcript'.

    async def get_media_transcript(self, doc_id: int) -> dict[str, Any] | None:
        row = await self.get_media_enrichment(doc_id, "transcript")
        if row is None:
            return None
        # Old call sites expect a `transcript` key, not `content`. Map back.
        return {**row, "transcript": row.get("content")}

    async def put_media_transcript(
        self,
        doc_id: int,
        transcript: str,
        model: str,
        duration_sec: int | None,
        language: str | None,
        cost_usd: float | None,
        file_sha1: str | None = None,
    ) -> None:
        await self.put_media_enrichment(
            doc_id,
            "transcript",
            transcript,
            model=model,
            cost_usd=cost_usd,
            duration_sec=duration_sec,
            language=language,
            file_sha1=file_sha1,
        )

    async def get_link_enrichment(self, url_hash: str) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT * FROM link_enrichments WHERE url_hash=?",
            (url_hash,),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_link_enrichment(
        self,
        url_hash: str,
        url: str,
        summary: str,
        *,
        title: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO link_enrichments(url_hash, url, summary, title, model, cost_usd, fetched_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                summary=excluded.summary,
                title=COALESCE(excluded.title, link_enrichments.title),
                model=excluded.model,
                cost_usd=excluded.cost_usd,
                fetched_at=excluded.fetched_at
            """,
            (url_hash, url, summary, title, model, cost_usd, _utcnow()),
        )
        await self._conn.commit()

    # ---------------------------------------------------------- YouTube cache

    async def get_youtube_video(self, video_id: str) -> dict[str, Any] | None:
        """Fetch a cached YouTube video row (metadata + transcript), or None."""
        cur = await self._conn.execute(
            "SELECT * FROM youtube_videos WHERE video_id=?",
            (video_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_youtube_video(
        self,
        *,
        video_id: str,
        url: str,
        title: str | None,
        channel_id: str | None,
        channel_title: str | None,
        channel_url: str | None,
        description: str | None,
        upload_date: str | None,
        duration_sec: int | None,
        view_count: int | None,
        like_count: int | None,
        tags: list[str] | None,
        language: str | None,
        transcript: str | None,
        transcript_source: str | None,
        transcript_model: str | None,
        transcript_cost_usd: float | None,
        transcript_timed: list[tuple[int, str]] | None = None,
    ) -> None:
        """Upsert a YouTube video row. `tags` flattens to a JSON array.

        `transcript_timed` is `[(start_sec, text), …]` from the captions
        track. Stored as JSON under `transcript_timed_json` so re-runs can
        rebuild time-stamped synthetic messages without re-fetching.
        """
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
        timed_json = json.dumps(transcript_timed, ensure_ascii=False) if transcript_timed else None
        now = _utcnow()
        transcribed_at = now if transcript else None
        await self._conn.execute(
            """
            INSERT INTO youtube_videos(
                video_id, url, title, channel_id, channel_title, channel_url,
                description, upload_date, duration_sec, view_count, like_count,
                tags, language, transcript, transcript_source, transcript_model,
                transcript_cost_usd, transcript_timed_json,
                fetched_at, transcribed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                channel_id=excluded.channel_id,
                channel_title=excluded.channel_title,
                channel_url=excluded.channel_url,
                description=excluded.description,
                upload_date=excluded.upload_date,
                duration_sec=excluded.duration_sec,
                view_count=excluded.view_count,
                like_count=excluded.like_count,
                tags=excluded.tags,
                language=excluded.language,
                transcript=COALESCE(excluded.transcript, youtube_videos.transcript),
                transcript_source=COALESCE(excluded.transcript_source, youtube_videos.transcript_source),
                transcript_model=COALESCE(excluded.transcript_model, youtube_videos.transcript_model),
                transcript_cost_usd=COALESCE(excluded.transcript_cost_usd, youtube_videos.transcript_cost_usd),
                transcript_timed_json=COALESCE(excluded.transcript_timed_json, youtube_videos.transcript_timed_json),
                fetched_at=excluded.fetched_at,
                transcribed_at=COALESCE(excluded.transcribed_at, youtube_videos.transcribed_at)
            """,
            (
                video_id,
                url,
                title,
                channel_id,
                channel_title,
                channel_url,
                description,
                upload_date,
                duration_sec,
                view_count,
                like_count,
                tags_json,
                language,
                transcript,
                transcript_source,
                transcript_model,
                transcript_cost_usd,
                timed_json,
                now,
                transcribed_at,
            ),
        )
        await self._conn.commit()

    async def has_youtube_transcript(self, video_id: str) -> bool:
        """Cheap exists-check for the cmd_analyze_youtube cache fast-path."""
        cur = await self._conn.execute(
            "SELECT 1 FROM youtube_videos WHERE video_id=? AND transcript IS NOT NULL LIMIT 1",
            (video_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return row is not None

    # ---------------------------------------------------------- Website cache

    async def get_website_page(self, page_id: str) -> dict[str, Any] | None:
        """Fetch a cached web page row (metadata + paragraphs), or None."""
        cur = await self._conn.execute(
            "SELECT * FROM website_pages WHERE page_id=?",
            (page_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_website_page(
        self,
        *,
        page_id: str,
        url: str,
        normalized_url: str,
        domain: str | None,
        title: str | None,
        site_name: str | None,
        author: str | None,
        published: str | None,
        language: str | None,
        word_count: int | None,
        paragraphs: list[str],
        content_hash: str,
        extractor: str | None,
        raw_html_size: int | None,
    ) -> None:
        """Upsert a website page row. `paragraphs` is JSON-encoded.

        Re-fetching the same URL with new content rewrites every column
        except `page_id` and bumps `fetched_at`. The new `content_hash`
        flows into `AnalysisOptions.options_payload`, so the analysis
        cache misses and re-runs.
        """
        paragraphs_json = json.dumps(paragraphs, ensure_ascii=False)
        await self._conn.execute(
            """
            INSERT INTO website_pages(
                page_id, url, normalized_url, domain, title, site_name,
                author, published, language, word_count, paragraphs_json,
                content_hash, extractor, raw_html_size, fetched_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                url=excluded.url,
                normalized_url=excluded.normalized_url,
                domain=excluded.domain,
                title=excluded.title,
                site_name=excluded.site_name,
                author=excluded.author,
                published=excluded.published,
                language=excluded.language,
                word_count=excluded.word_count,
                paragraphs_json=excluded.paragraphs_json,
                content_hash=excluded.content_hash,
                extractor=excluded.extractor,
                raw_html_size=excluded.raw_html_size,
                fetched_at=excluded.fetched_at
            """,
            (
                page_id,
                url,
                normalized_url,
                domain,
                title,
                site_name,
                author,
                published,
                language,
                word_count,
                paragraphs_json,
                content_hash,
                extractor,
                raw_html_size,
                _utcnow(),
            ),
        )
        await self._conn.commit()

    # ----------------------------------------------- local files

    async def get_local_file(self, file_id: str) -> dict[str, Any] | None:
        """Fetch a cached local-file row (metadata + paragraphs), or None."""
        cur = await self._conn.execute(
            "SELECT * FROM local_files WHERE file_id=?",
            (file_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def put_local_file(
        self,
        *,
        file_id: str,
        abs_path: str,
        name: str,
        kind: str,
        extension: str | None,
        content_hash: str,
        paragraphs: list[str],
        extract_size: int | None,
    ) -> None:
        """Upsert a local-file row. Mirrors `put_website_page` exactly so
        the file path and the website path share the same caching shape
        in the analyzer (just a different table to look up by id).
        """
        paragraphs_json = json.dumps(paragraphs, ensure_ascii=False)
        await self._conn.execute(
            """
            INSERT INTO local_files(
                file_id, abs_path, name, kind, extension,
                content_hash, paragraphs_json, extract_size, fetched_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                abs_path=excluded.abs_path,
                name=excluded.name,
                kind=excluded.kind,
                extension=excluded.extension,
                content_hash=excluded.content_hash,
                paragraphs_json=excluded.paragraphs_json,
                extract_size=excluded.extract_size,
                fetched_at=excluded.fetched_at
            """,
            (
                file_id,
                abs_path,
                name,
                kind,
                extension,
                content_hash,
                paragraphs_json,
                extract_size,
                _utcnow(),
            ),
        )
        await self._conn.commit()

    # ----------------------------------------------- last-run-args (wizard)

    async def put_last_run_args(
        self,
        chat_id: int,
        thread_id: int,
        args: dict[str, Any],
    ) -> None:
        """Persist a CLI kwargs dict so the wizard can offer 'Repeat last run'.

        Upserts on `(chat_id, thread_id)` — only the most recent run is
        kept. Pruning Path/datetime/EnrichOpts to JSON-safe scalars is the
        caller's job (we just `json.dumps` what we get).
        """
        from json import dumps as _dumps

        await self._conn.execute(
            """
            INSERT INTO chat_last_run_args(chat_id, thread_id, args_json, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                args_json=excluded.args_json,
                updated_at=excluded.updated_at
            """,
            (chat_id, thread_id, _dumps(args, ensure_ascii=False), _utcnow()),
        )
        await self._conn.commit()

    async def get_last_run_args(
        self,
        chat_id: int,
        thread_id: int = 0,
    ) -> dict[str, Any] | None:
        """Inverse of `put_last_run_args`. Returns the stored kwargs or None."""
        from json import loads as _loads

        cur = await self._conn.execute(
            "SELECT args_json, updated_at FROM chat_last_run_args WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id),
        )
        row = await cur.fetchone()
        await cur.close()
        if not row:
            return None
        try:
            args = _loads(row["args_json"])
        except Exception:
            return None
        if not isinstance(args, dict):
            return None
        args["__updated_at"] = row["updated_at"]
        return args

    # ------------------------------------------------------- app_settings

    async def get_app_setting(self, key: str) -> str | None:
        """Return the saved override for `key`, or None if unset.

        `None` means "no row" — `apply_db_locale_overrides` distinguishes
        this from an empty-string row, which means "explicitly cleared".
        """
        cur = await self._conn.execute("SELECT value FROM app_settings WHERE key=?", (key,))
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return row["value"]

    async def set_app_setting(self, key: str, value: str) -> None:
        """Upsert a setting override. `value=""` is allowed (means cleared).

        Validates ``key`` against the allowlist defined in
        :data:`unread.db._keys.OVERRIDE_KEYS` so a typo can't silently
        store a never-read value (the bootstrap reads only allowlisted
        keys, so an unknown key is dead weight). Unknown keys raise
        ``ValueError``; the settings UI catches that and surfaces a
        "did you mean…?" suggestion.
        """
        if key not in _OVERRIDE_KEYS:
            raise ValueError(f"unknown setting key: {key!r}; allowed: {sorted(_OVERRIDE_KEYS)}")
        await self._conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, _utcnow()),
        )
        await self._conn.commit()

    async def delete_app_setting(self, key: str) -> bool:
        """Remove an override entirely so the next read falls back to config.

        Returns True if a row was deleted.
        """
        cur = await self._conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
        await self._conn.commit()
        deleted = cur.rowcount or 0
        await cur.close()
        return deleted > 0

    async def get_all_app_settings(self) -> dict[str, str]:
        """Return all saved user overrides as a `{key: value}` dict.

        Filters out internal `_meta.*` rows (e.g. the schema-version
        stamp) so external consumers — settings UI, doctor, tests —
        only see user-facing config knobs.
        """
        cur = await self._conn.execute("SELECT key, value FROM app_settings")
        rows = await cur.fetchall()
        await cur.close()
        return {row["key"]: row["value"] for row in rows if not row["key"].startswith("_meta.")}

    async def clear_all_app_settings(self) -> int:
        """Remove every user override. Returns the number of rows deleted.

        Preserves internal `_meta.*` rows (schema version stamp etc.)
        so a user-driven "reset all settings" doesn't accidentally
        wipe the version stamp and re-trigger a future-DB check the
        next time the repo is opened.
        """
        cur = await self._conn.execute("DELETE FROM app_settings WHERE key NOT LIKE '\\_meta.%' ESCAPE '\\'")
        await self._conn.commit()
        deleted = cur.rowcount or 0
        await cur.close()
        return deleted

    # ------------------------------------------------------- secrets

    async def get_secrets(self) -> dict[str, str]:
        """Return persisted secrets keyed by allowlisted name.

        Filters out any rows that don't match the allowlist — defensive
        against schema additions / manual SQL inserts that aren't part
        of the documented set.
        """
        cur = await self._conn.execute("SELECT key, value FROM secrets")
        rows = await cur.fetchall()
        await cur.close()
        return {row["key"]: row["value"] for row in rows if row["key"] in _SECRET_KEYS}

    async def put_secrets(self, values: dict[str, str]) -> None:
        """Upsert a subset of secrets in one transaction.

        Empty / falsy values are skipped — passing
        ``{"telegram.api_hash": ""}`` is a no-op rather than wiping the
        existing row. To explicitly clear a secret, call
        :meth:`delete_secret`. Unknown keys (outside the allowlist) are
        rejected with a ``ValueError`` so a typo can't silently grow
        the secrets table.
        """
        for key in values:
            if key not in _SECRET_KEYS:
                raise ValueError(f"unknown secret key: {key!r}; allowed: {sorted(_SECRET_KEYS)}")
        rows = [(k, v, _utcnow()) for k, v in values.items() if v]
        if not rows:
            return
        await self._conn.executemany(
            """
            INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        await self._conn.commit()

    async def delete_secret(self, key: str) -> bool:
        """Remove one secret row. Returns True iff a row existed."""
        if key not in _SECRET_KEYS:
            raise ValueError(f"unknown secret key: {key!r}")
        cur = await self._conn.execute("DELETE FROM secrets WHERE key=?", (key,))
        await self._conn.commit()
        deleted = cur.rowcount or 0
        await cur.close()
        return deleted > 0

    # ------------------------------------------------------- embeddings

    async def msg_ids_missing_embedding(
        self,
        chat_id: int,
        model: str,
    ) -> list[int]:
        """Return msg_ids for `chat_id` that have a body but no embedding for `model`.

        "Body" = `text` non-empty OR `transcript` non-empty. Empty media-only
        rows are skipped — embedding "(no body)" wastes the API call.
        """
        sql = """
            SELECT msg_id FROM messages
            WHERE chat_id = ?
              AND ((text IS NOT NULL AND text != '') OR (transcript IS NOT NULL AND transcript != ''))
              AND msg_id NOT IN (
                  SELECT msg_id FROM message_embeddings
                  WHERE chat_id = ? AND model = ?
              )
            ORDER BY msg_id ASC
        """
        cur = await self._conn.execute(sql, (chat_id, chat_id, model))
        rows = await cur.fetchall()
        await cur.close()
        return [int(r["msg_id"]) for r in rows]

    async def put_embeddings(
        self,
        rows: list[tuple[int, int, str, bytes]],
    ) -> int:
        """Bulk-insert `(chat_id, msg_id, model, vector_bytes)` rows.

        `INSERT OR REPLACE` so re-embedding the same message under the same
        model overwrites — useful when an upstream change (transcript
        added) means the body changed.
        """
        if not rows:
            return 0
        now = _utcnow()
        await self._conn.executemany(
            """
            INSERT OR REPLACE INTO message_embeddings(chat_id, msg_id, model, vector, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            [(*r, now) for r in rows],
        )
        await self._conn.commit()
        return len(rows)

    async def get_embeddings(
        self,
        chat_ids: list[int],
        model: str,
    ) -> list[tuple[int, int, bytes]]:
        """Return `(chat_id, msg_id, vector_bytes)` for the scoped chats + model."""
        if not chat_ids:
            return []
        placeholders = ",".join("?" for _ in chat_ids)
        sql = (
            f"SELECT chat_id, msg_id, vector FROM message_embeddings "
            f"WHERE model = ? AND chat_id IN ({placeholders})"
        )
        cur = await self._conn.execute(sql, (model, *chat_ids))
        rows = await cur.fetchall()
        await cur.close()
        return [(int(r["chat_id"]), int(r["msg_id"]), bytes(r["vector"])) for r in rows]

    # ------------------------------------------------------------- analysis

    async def cache_get(self, batch_hash: str) -> dict[str, Any] | None:
        cur = await self._conn.execute("SELECT * FROM analysis_cache WHERE batch_hash=?", (batch_hash,))
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def cache_put(
        self,
        batch_hash: str,
        preset: str,
        model: str,
        prompt_version: str,
        result: str,
        prompt_tokens: int | None,
        cached_tokens: int | None,
        completion_tokens: int | None,
        cost_usd: float | None,
        truncated: bool = False,
    ) -> None:
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO analysis_cache(batch_hash, preset, model, prompt_version,
                result, prompt_tokens, cached_tokens, completion_tokens, cost_usd,
                truncated, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_hash,
                preset,
                model,
                prompt_version,
                result,
                prompt_tokens,
                cached_tokens,
                completion_tokens,
                cost_usd,
                1 if truncated else 0,
                _utcnow(),
            ),
        )
        await self._conn.commit()

    async def cache_purge(
        self,
        older_than_days: int | None = None,
        preset: str | None = None,
        model: str | None = None,
    ) -> int:
        if older_than_days is not None and older_than_days <= 0:
            return 0
        sql = "DELETE FROM analysis_cache WHERE 1=1"
        args: list[Any] = []
        if older_than_days is not None:
            sql += " AND datetime(created_at) < datetime('now', ?)"
            args.append(f"-{older_than_days} days")
        if preset:
            sql += " AND preset=?"
            args.append(preset)
        if model:
            sql += " AND model=?"
            args.append(model)
        cur = await self._conn.execute(sql, args)
        await self._conn.commit()
        return cur.rowcount or 0

    async def cache_stats(self) -> dict[str, Any]:
        """Summary of analysis_cache: totals + per-(preset, model) breakdown."""
        cur = await self._conn.execute(
            """
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(LENGTH(result)), 0) AS result_bytes,
                   COALESCE(SUM(cost_usd), 0) AS saved_cost_usd,
                   MIN(created_at) AS oldest,
                   MAX(created_at) AS newest
            FROM analysis_cache
            """
        )
        totals = await cur.fetchone()
        await cur.close()
        cur = await self._conn.execute(
            """
            SELECT preset, model,
                   COUNT(*) AS rows,
                   COALESCE(SUM(LENGTH(result)), 0) AS result_bytes,
                   COALESCE(SUM(cost_usd), 0) AS saved_cost_usd
            FROM analysis_cache
            GROUP BY preset, model
            ORDER BY rows DESC
            """
        )
        by_group = [dict(r) for r in await cur.fetchall()]
        await cur.close()
        return {
            "rows": int(totals["rows"] or 0),
            "result_bytes": int(totals["result_bytes"] or 0),
            "saved_cost_usd": float(totals["saved_cost_usd"] or 0.0),
            "oldest": totals["oldest"],
            "newest": totals["newest"],
            "by_group": by_group,
        }

    async def cache_list(
        self,
        preset: str | None = None,
        model: str | None = None,
        older_than_days: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lightweight listing (no result body) for preview/ls."""
        sql = (
            "SELECT batch_hash, preset, model, prompt_version,"
            " prompt_tokens, cached_tokens, completion_tokens, cost_usd,"
            " LENGTH(result) AS result_bytes, created_at"
            " FROM analysis_cache WHERE 1=1"
        )
        args: list[Any] = []
        if older_than_days:
            sql += " AND datetime(created_at) < datetime('now', ?)"
            args.append(f"-{older_than_days} days")
        if preset:
            sql += " AND preset=?"
            args.append(preset)
        if model:
            sql += " AND model=?"
            args.append(model)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def cache_iter_full(
        self,
        preset: str | None = None,
        model: str | None = None,
        older_than_days: int | None = None,
    ) -> list[dict[str, Any]]:
        """Full rows incl. result — for export."""
        sql = "SELECT * FROM analysis_cache WHERE 1=1"
        args: list[Any] = []
        if older_than_days:
            sql += " AND datetime(created_at) < datetime('now', ?)"
            args.append(f"-{older_than_days} days")
        if preset:
            sql += " AND preset=?"
            args.append(preset)
        if model:
            sql += " AND model=?"
            args.append(model)
        sql += " ORDER BY created_at DESC"
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def vacuum(self) -> int:
        """Run VACUUM and return reclaimed bytes (file-size delta)."""
        before = self._path.stat().st_size if self._path.exists() else 0
        await self._conn.commit()
        await self._conn.execute("VACUUM")
        after = self._path.stat().st_size if self._path.exists() else 0
        return max(0, before - after)

    async def backup_to(self, dest: Path) -> int:
        """Write a consistent point-in-time copy to `dest` and return its size.

        Uses SQLite's `VACUUM INTO` — atomic from the writer's perspective, and
        the result is itself a freshly compacted DB (no WAL leftovers, no
        free pages). Safe to run while other connections write to the source;
        the new file is opened only after the copy finishes.
        """
        await self._conn.commit()
        dest.parent.mkdir(parents=True, exist_ok=True)
        # SQLite refuses VACUUM INTO if the target exists. Quote the path
        # for safety (escape single quotes by doubling per SQL convention).
        target = str(dest).replace("'", "''")
        await self._conn.execute(f"VACUUM INTO '{target}'")
        return dest.stat().st_size if dest.exists() else 0

    async def record_run(
        self,
        chat_id: int,
        thread_id: int,
        preset: str,
        from_date: datetime | None,
        to_date: datetime | None,
        msg_count: int,
        chunk_count: int,
        batch_hashes: Sequence[str],
        final_result: str,
        total_cost_usd: float,
    ) -> int:
        cur = await self._conn.execute(
            """
            INSERT INTO analysis_runs(chat_id, thread_id, preset, from_date, to_date,
                msg_count, chunk_count, batch_hashes, final_result, total_cost_usd, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                thread_id,
                preset,
                from_date.isoformat() if from_date else None,
                to_date.isoformat() if to_date else None,
                msg_count,
                chunk_count,
                json.dumps(list(batch_hashes)),
                final_result,
                total_cost_usd,
                _utcnow(),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid or 0

    # ----------------------------------------------------------------- usage

    async def log_usage(
        self,
        kind: str,
        model: str,
        prompt_tokens: int | None = None,
        cached_tokens: int | None = None,
        completion_tokens: int | None = None,
        audio_seconds: int | None = None,
        cost_usd: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Insert a usage row; shielded against task cancellation.

        By the time a caller invokes this, the LLM call has already
        cost real money. A Ctrl-C between the API response and the DB
        commit would otherwise abort the INSERT and the spend would
        never appear in `unread stats`. We shield the INSERT so it
        still runs even if the caller is being torn down, then
        re-raise the cancellation so the rest of the pipeline unwinds.
        """
        coro = self._do_log_usage(
            kind=kind,
            model=model,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            audio_seconds=audio_seconds,
            cost_usd=cost_usd,
            context=context,
        )
        task = asyncio.ensure_future(coro)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Inner write is still in flight. Wait briefly for it to
            # land, then bubble up so callers stop on schedule.
            import contextlib as _cl

            with _cl.suppress(TimeoutError, Exception):
                await asyncio.wait_for(task, timeout=2.0)
            raise

    async def _do_log_usage(
        self,
        *,
        kind: str,
        model: str,
        prompt_tokens: int | None,
        cached_tokens: int | None,
        completion_tokens: int | None,
        audio_seconds: int | None,
        cost_usd: float | None,
        context: dict[str, Any] | None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO usage_log(kind, model, prompt_tokens, cached_tokens, completion_tokens,
                audio_seconds, cost_usd, context, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                model,
                prompt_tokens,
                cached_tokens,
                completion_tokens,
                audio_seconds,
                cost_usd,
                json.dumps(context) if context else None,
                _utcnow(),
            ),
        )
        await self._conn.commit()

    async def stats_by(
        self,
        group_by: str = "preset",
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate usage_log rows. `group_by`: chat, preset, model, day, kind."""
        group_cols = {
            "preset": "json_extract(context, '$.preset')",
            "chat": "json_extract(context, '$.chat_id')",
            "model": "model",
            "day": "date(created_at)",
            "kind": "kind",
        }
        col = group_cols.get(group_by, "model")
        # `unpriced_calls` counts rows whose `cost_usd` is NULL — that
        # happens when a model isn't listed in the pricing table. Without
        # this, SUM(cost_usd) silently under-reports spend for every
        # unknown-model row and the user has no way to tell.
        sql = f"""
            SELECT {col} AS bucket,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(audio_seconds), 0) AS audio_seconds,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM usage_log
            WHERE 1=1
        """
        args: list[Any] = []
        if since:
            sql += " AND created_at >= ?"
            args.append(since.isoformat())
        sql += " GROUP BY bucket ORDER BY cost_usd DESC"
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def cache_effectiveness(
        self,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Per-(chat, preset) cache hit-rate over `usage_log` rows of kind='chat'.

        Hit-rate uses the prompt-cache-tokens / prompt-tokens ratio (server-
        side OpenAI cache), not the analysis_cache table — that's the bigger
        cost saver in practice. `analysis_cache` itself is invisible to the
        usage_log because zero-cost cache hits aren't logged.

        Returns rows sorted by total_calls descending so the biggest spenders
        bubble up. Empty rows (no usage logged) → empty list.
        """
        sql = """
            SELECT
                COALESCE(json_extract(context, '$.chat_id'), 'unknown') AS chat_id,
                COALESCE(json_extract(context, '$.preset'), 'unknown') AS preset,
                COUNT(*) AS total_calls,
                SUM(CASE WHEN cached_tokens > 0 THEN 1 ELSE 0 END) AS hit_calls,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM usage_log
            WHERE kind = 'chat'
        """
        args: list[Any] = []
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(since.isoformat())
        sql += " GROUP BY chat_id, preset ORDER BY total_calls DESC"
        cur = await self._conn.execute(sql, args)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def cache_hit_rate(self, since: datetime | None = None) -> float:
        sql = "SELECT SUM(cached_tokens) AS c, SUM(prompt_tokens) AS p FROM usage_log WHERE kind='chat'"
        args: list[Any] = []
        if since:
            sql += " AND created_at >= ?"
            args.append(since.isoformat())
        cur = await self._conn.execute(sql, args)
        row = await cur.fetchone()
        await cur.close()
        if not row or not row["p"]:
            return 0.0
        return float(row["c"] or 0) / float(row["p"])


@asynccontextmanager
async def open_repo(path: Path | str) -> AsyncIterator[Repo]:
    repo = await Repo.open(path)
    # Apply any user-saved overrides from app_settings on top of the
    # config.toml-driven settings singleton. Lets `unread settings` persist
    # locale / audio-language preferences without touching config files.
    await _apply_db_overrides(repo)
    try:
        yield repo
    finally:
        await repo.close()


# Allow-list of `secrets` and `app_settings` keys. Defined once in
# `unread.db._keys` so the wizard, the per-connect overlay, the sync
# bootstrap reader, and the legacy fallback all see the same set.
#
# To add a new override key:
#   1. Append it to `_keys.OVERRIDE_KEYS`.
#   2. Add a branch in both `apply_db_overrides_sync` (sync, used at CLI
#      bootstrap) and `_apply_db_overrides` (async, used per-`open_repo`).
#      Mind the value type — bool / int / str — and any sentinel rules
#      (e.g. empty string for "autodetect").
#   3. Surface it in `unread/settings/commands.py:_SETTING_DEFS` so
#      the interactive editor can show + edit it.
from unread.db._keys import OVERRIDE_KEYS as _OVERRIDE_KEYS  # noqa: E402
from unread.db._keys import SECRET_KEYS as _SECRET_KEYS  # noqa: E402


def _coerce_bool(raw: str) -> bool | None:
    """Parse a stored bool override. None on garbage so the caller can
    leave the existing value alone instead of silently flipping it."""
    s = raw.strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def _coerce_int(raw: str) -> int | None:
    s = raw.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _apply_one_override(settings, key: str, value: str) -> None:
    """Apply a single (key, value) pair onto the live settings singleton.

    Centralises the type coercion + setattr logic so the sync and async
    overlay paths can share it instead of duplicating every branch.
    Defensive: unknown / malformed values are silently ignored — the
    config-default stays in effect.
    """
    # Languages — strings; empty string for content_language means
    # "follow locale.language", for audio_language means "autodetect".
    if key == "locale.language" and value:
        settings.locale.language = value
        return
    if key == "locale.content_language":
        settings.locale.content_language = value
        return
    if key == "openai.audio_language":
        settings.openai.audio_language = value or None
        return
    # Model name overrides — empty value clears the override (rare).
    if key in {
        "openai.chat_model_default",
        "openai.filter_model_default",
        "openai.audio_model_default",
        "enrich.vision_model",
    }:
        if not value:
            return
        section, attr = key.split(".", 1)
        setattr(getattr(settings, section), attr, value)
        return
    # Enrich bool toggles.
    if key in {
        "enrich.voice",
        "enrich.videonote",
        "enrich.video",
        "enrich.image",
        "enrich.doc",
        "enrich.link",
    }:
        b = _coerce_bool(value)
        if b is None:
            return
        attr = key.split(".", 1)[1]
        setattr(settings.enrich, attr, b)
        return
    # Analyze tuning.
    if key == "analyze.high_impact_reactions":
        n = _coerce_int(value)
        if n is None or n < 0:
            return
        settings.analyze.high_impact_reactions = n
        return
    if key == "analyze.dedupe_forwards":
        b = _coerce_bool(value)
        if b is None:
            return
        settings.analyze.dedupe_forwards = b
        return
    if key == "analyze.min_msg_chars":
        n = _coerce_int(value)
        if n is None or n < 0:
            return
        settings.analyze.min_msg_chars = n
        return
    if key == "analyze.plain_citations":
        b = _coerce_bool(value)
        if b is None:
            return
        settings.analyze.plain_citations = b
        return
    # AI provider routing — strings, empty value resets to default.
    if key == "ai.provider":
        if value:
            settings.ai.provider = value
        return
    if key in {"ai.base_url", "ai.chat_model", "ai.filter_model"}:
        attr = key.split(".", 1)[1]
        setattr(settings.ai, attr, value)
        return
    if key == "local.base_url":
        if value:
            settings.local.base_url = value
        return


def apply_db_overrides_sync(settings, db_path: Path | str | None = None) -> None:
    """Sync flavour of `_apply_db_overrides` for CLI bootstrap.

    Used in `cli.py` at module-import time (before Typer constructs the
    app and reads `help=` strings) so `i18n.t()` lookups in help text
    pick up the user's saved `locale.language`. Uses stdlib `sqlite3`
    instead of aiosqlite — there's no event loop yet at import time.

    Defensive on every error: a missing DB / unreadable file / missing
    table all degrade to "leave settings as-is", so the CLI never fails
    to construct just because the user hasn't run anything yet. Reads
    only — never writes — so concurrent commands are safe.
    """
    import sqlite3

    target = Path(db_path) if db_path is not None else Path(settings.storage.data_path)
    if not target.is_file():
        return
    try:
        # `mode=ro` opens read-only without creating the file; relative
        # paths inside `file:` URIs don't resolve against cwd, so go
        # absolute. We deliberately avoid `nolock=1` — sqlite refuses to
        # combine it with WAL journals (the default in this DB), failing
        # to open with "unable to open database file".
        absolute = target.resolve()
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return
    try:
        cur = conn.execute("SELECT key, value FROM app_settings")
        rows = dict(cur.fetchall())
    except sqlite3.Error:
        # Table missing (pre-migration DB) or any other read failure.
        conn.close()
        return
    conn.close()
    if not rows:
        return
    for key in _OVERRIDE_KEYS:
        if key in rows:
            _apply_one_override(settings, key, rows[key])


def read_data_db_secrets_sync(db_path: Path | str) -> dict[str, str]:
    """Read persisted secrets from `data.sqlite` without an event loop.

    Used at `load_settings` time to overlay api_id / api_hash /
    api_key when env / `.env` left them empty. Mirrors the defensive
    shape of `apply_db_overrides_sync`: read-only URI open, every
    sqlite error degrades to an empty dict so the CLI never fails to
    construct because the user hasn't run a command yet.

    Allowlist filtering happens here too — a manual `INSERT` of an
    unknown key never leaks back into settings.
    """
    import sqlite3

    target = Path(db_path)
    if not target.is_file():
        return {}
    try:
        absolute = target.resolve()
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return {}
    try:
        cur = conn.execute("SELECT key, value FROM secrets")
        rows = dict(cur.fetchall())
    except sqlite3.Error:
        conn.close()
        return {}
    conn.close()
    return {k: v for k, v in rows.items() if k in _SECRET_KEYS and v}


async def _apply_db_overrides(repo: Repo) -> None:
    """Overlay saved `app_settings` onto the live `get_settings()` singleton.

    Called once per `open_repo`. Idempotent: re-applying with the same DB
    rows produces the same settings. Settings the user hasn't saved are
    untouched (config.toml / defaults still win).
    """
    try:
        rows = await repo.get_all_app_settings()
    except Exception:
        # Pre-migration DB or transient error — don't take down the whole
        # CLI just because the overrides table isn't ready yet.
        return
    if not rows:
        return
    from unread.config import get_settings

    s = get_settings()
    for key in _OVERRIDE_KEYS:
        if key in rows:
            _apply_one_override(s, key, rows[key])
