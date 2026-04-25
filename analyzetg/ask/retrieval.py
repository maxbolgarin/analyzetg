"""Keyword retrieval over the local message store.

Approach: tokenize the question, drop short tokens / stop words, build a
parameterised SQL query that scores matches by hit count, filter by
chat/thread/folder/period, take the top-N, then re-sort chronologically
so the LLM sees the conversation in order.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from analyzetg.db.repo import Repo
from analyzetg.models import Message

# Bilingual stop list — only the highest-frequency throwaways. Short tokens
# (< 3 chars) are dropped separately.
_STOP_WORDS: frozenset[str] = frozenset(
    [
        # English
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "from",
        "with",
        "about",
        "as",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "should",
        "could",
        "what",
        "who",
        "where",
        "when",
        "why",
        "how",
        "which",
        # Russian
        "и",
        "в",
        "на",
        "не",
        "что",
        "это",
        "как",
        "по",
        "из",
        "от",
        "за",
        "для",
        "уже",
        "ещё",
        "еще",
        "так",
        "там",
        "тут",
        "был",
        "была",
        "было",
        "были",
        "есть",
        "нет",
        "или",
        "то",
        "же",
        "да",
        "но",
        "о",
    ]
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokenize_question(question: str) -> list[str]:
    """Extract content tokens from the user's question.

    Lowercased, deduped (preserving first-seen order), shorter-than-3-char
    tokens dropped, stop words removed. We keep digits and Unicode word
    chars so phone numbers / hashtags survive.
    """
    seen: dict[str, None] = {}
    for raw in _TOKEN_RE.findall(question.lower()):
        if len(raw) < 3:
            continue
        if raw in _STOP_WORDS:
            continue
        if raw not in seen:
            seen[raw] = None
    return list(seen.keys())


async def retrieve_messages(
    *,
    repo: Repo,
    question: str,
    chat_ids: list[int] | None = None,
    thread_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
    return_scores: bool = False,
) -> list[Message] | list[tuple[Message, int]]:
    """Return the top-N messages most relevant to `question`.

    Scoring: number of distinct tokens that LIKE-match the message body
    (text or transcript). Ties broken by recency (newer wins). Output is
    chronologically sorted so the LLM sees the timeline in order.

    `return_scores=True` returns `list[(Message, score)]` instead of
    bare messages — used by `--show-retrieved` and the rerank pass that
    care about the relevance ranking.

    Returns `[]` if the question contains no usable tokens — caller
    surfaces a "rephrase your question" hint to the user.
    """
    tokens = tokenize_question(question)
    if not tokens:
        return []

    # Build (col LIKE ?) clauses against `text` (and `transcript` for voice
    # rows). One bound parameter per token; SQLite will do a single pass
    # per clause but the per-row work is tiny.
    body_expr = "(COALESCE(text, '') || ' ' || COALESCE(transcript, ''))"
    score_terms: list[str] = []
    score_args: list[Any] = []
    where_terms: list[str] = []
    where_args: list[Any] = []
    for tok in tokens:
        like = f"%{tok}%"
        # Each matched token contributes 1 to the score.
        score_terms.append(f"(CASE WHEN {body_expr} LIKE ? THEN 1 ELSE 0 END)")
        score_args.append(like)
        where_terms.append(f"{body_expr} LIKE ?")
        where_args.append(like)

    where_sql = "(" + " OR ".join(where_terms) + ")"
    args: list[Any] = list(score_args) + list(where_args)

    if chat_ids:
        placeholders = ",".join("?" for _ in chat_ids)
        where_sql += f" AND chat_id IN ({placeholders})"
        args.extend(chat_ids)
    if thread_id is not None:
        where_sql += " AND (thread_id = ? OR (? = 0 AND thread_id IS NULL))"
        args.extend([thread_id, thread_id])
    if since is not None:
        where_sql += " AND date >= ?"
        args.append(since.isoformat())
    if until is not None:
        where_sql += " AND date <= ?"
        args.append(until.isoformat())

    score_sql = " + ".join(score_terms)
    sql = f"""
        SELECT *, ({score_sql}) AS _score
        FROM messages
        WHERE {where_sql}
        ORDER BY _score DESC, date DESC
        LIMIT ?
    """
    args.append(limit)

    cur = await repo._conn.execute(sql, args)
    rows = await cur.fetchall()
    await cur.close()

    pairs: list[tuple[Message, int]] = [(repo._row_to_msg(r), int(r["_score"] or 0)) for r in rows]
    # Re-sort chronologically: the LLM reads the conversation in order,
    # which improves answer coherence vs. relevance-ordered chunks.
    pairs.sort(key=lambda p: (p[0].chat_id, p[0].date or datetime.min, p[0].msg_id))
    if return_scores:
        return pairs
    return [m for m, _ in pairs]
