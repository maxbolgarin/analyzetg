"""Cheap-model rerank between keyword retrieval and the flagship answer.

Why two stages: keyword retrieval is precise on terms but blind to paraphrase
("postgres" vs "the DB"). Throwing 200–500 keyword hits at the flagship costs
real money. Inserting a cheap rerank pass (gpt-5.4-nano scoring 1–5 per
message) on the candidate pool, then keeping the top-K for the flagship,
slashes per-question cost ~5–10× while typically *improving* answer quality
because the cheap model also catches paraphrase.

Single LLM call shape per batch: system asks for a JSON list of
`{"msg_id": int, "score": int}` ratings; we parse, merge, sort, take the
top-K. Failures (parse errors, missing rows) silently skip — rerank is
best-effort, retrieval already produced a usable pool.
"""

from __future__ import annotations

import json
import re
from typing import Any

from unread.analyzer.formatter import format_messages
from unread.analyzer.openai_client import build_messages, chat_complete, make_client
from unread.db.repo import Repo
from unread.models import Message
from unread.util.logging import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    "You rate Telegram messages by how directly they answer a user's question. "
    "Output ONE valid JSON array, nothing else. Each element: "
    '{"msg_id": <int>, "score": <int 1-5>}. '
    "5 = directly answers; 4 = strongly relevant context; 3 = related; "
    "2 = weakly related; 1 = unrelated. Score every msg_id you see, exactly once."
)

# Tolerant JSON-array extraction: the model sometimes wraps the array in
# prose ("Here's the JSON:") or in a ```json fence. Pull the first balanced
# `[…]` chunk.
_JSON_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)


async def rerank(
    *,
    repo: Repo,
    pool: list[tuple[Message, int]],
    question: str,
    model: str,
    keep: int,
    batch_size: int = 50,
) -> list[tuple[Message, int]]:
    """Rerank a keyword-retrieval pool down to `keep` items.

    `pool` is `(Message, keyword_score)` from `retrieve_messages(...,
    return_scores=True)`. Returns the top-`keep` by rerank score, with the
    rerank score replacing the keyword score in the tuple. Falls back to
    the input pool (truncated to `keep`) on any rerank failure.
    """
    if not pool or len(pool) <= keep:
        return pool[:keep]

    oai = make_client()
    rerank_scores: dict[int, int] = {}  # msg_id → rerank score

    for batch_start in range(0, len(pool), batch_size):
        batch = pool[batch_start : batch_start + batch_size]
        msgs = [m for m, _ in batch]
        formatted = format_messages(msgs)
        user = (
            f"Question: {question.strip()}\n\n"
            f"Messages to rate ({len(msgs)}):\n\n{formatted}\n\n"
            "JSON array of ratings:"
        )
        try:
            res = await chat_complete(
                oai,
                repo=repo,
                model=model,
                messages=build_messages(_SYSTEM_PROMPT, "", user),
                max_tokens=1000,
                context={"phase": "ask_rerank", "batch": batch_start // batch_size},
            )
        except Exception as e:
            log.warning("rerank.batch_failed", batch=batch_start, err=str(e)[:200])
            continue

        ratings = _parse_ratings(res.text)
        if not ratings:
            log.warning("rerank.batch_empty", batch=batch_start, text=(res.text or "")[:200])
            continue
        for msg_id, score in ratings:
            # Last-write-wins on duplicate msg_id (model glitches, pool overlap).
            rerank_scores[msg_id] = score

    if not rerank_scores:
        # Total failure — return the keyword pool truncated. Sort by the
        # keyword score first so we keep the *best* keyword hits, not an
        # arbitrary slice. Caller still gets a usable pool, just without
        # the rerank improvement.
        sorted_pool = sorted(pool, key=lambda p: -p[1])
        return sorted_pool[:keep]

    # Score-sort, take top-`keep`. Messages the model didn't rate get
    # treated as score=0 so they fall to the bottom.
    annotated: list[tuple[Message, int]] = [(m, rerank_scores.get(m.msg_id, 0)) for m, _ in pool]
    annotated.sort(key=lambda p: -p[1])
    return annotated[:keep]


def _parse_ratings(text: str | None) -> list[tuple[int, int]]:
    """Extract `[(msg_id, score), …]` from the model's response.

    Tolerates: leading/trailing prose, ```json fences, trailing commas
    (json5-ish glitches the cheap model occasionally produces).
    """
    if not text:
        return []
    # Try direct parse first; common case.
    candidates: list[Any] = []
    raw = text.strip()
    if raw.startswith("```"):
        # Strip a single fenced block.
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        candidates = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(text)
        if not m:
            return []
        try:
            candidates = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(candidates, list):
        return []
    out: list[tuple[int, int]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        msg_id = item.get("msg_id")
        score = item.get("score")
        if not isinstance(msg_id, int) or not isinstance(score, int):
            continue
        out.append((msg_id, max(1, min(5, score))))
    return out
