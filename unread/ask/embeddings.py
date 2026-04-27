"""OpenAI-embedding-backed retrieval for `unread ask --semantic`.

Index lifecycle:
  - `unread ask --build-index --chat <ref>` (or `--folder`) walks the local DB,
    finds messages without an embedding row, batches them, calls the
    embeddings API, and persists vectors.
  - `unread ask --semantic …` embeds the question, cosine-similarity-scans the
    rows for the scoped chats, returns top-K.

Storage shape: `message_embeddings(chat_id, msg_id, model, vector BLOB,
created_at)`. Vector is `array.array('f', floats).tobytes()` — float32,
6KB per row at 1536 dims (text-embedding-3-small).

No FAISS / hnswlib: a few-thousand-row corpus does cosine in numpy in
single-digit ms. If a user pushes >100k embedded messages and queries get
slow, we add an ANN index then.
"""

from __future__ import annotations

import array
from typing import TYPE_CHECKING

from unread.db.repo import Repo
from unread.models import Message
from unread.util.logging import get_logger

if TYPE_CHECKING:
    from openai import AsyncOpenAI

log = get_logger(__name__)

DEFAULT_EMBED_MODEL = "text-embedding-3-small"
_EMBED_BATCH = 100  # OpenAI embeddings allow up to 2048 inputs per call; 100 stays well under.


def _vec_to_bytes(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _bytes_to_vec(b: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(b)
    return a


def _body_for_embedding(m: Message) -> str:
    """Body text used for one message's embedding.

    Mirrors what `unread ask` retrieval scans: text + transcript. Image
    descriptions / link summaries / doc extracts live in separate tables
    and aren't searchable today; folding them in is a future extension.
    """
    parts: list[str] = []
    if m.text:
        parts.append(m.text)
    if m.transcript:
        parts.append(m.transcript)
    return "\n".join(parts).strip()


async def _embed_batch(oai: AsyncOpenAI, model: str, inputs: list[str]) -> list[list[float]]:
    """One OpenAI embeddings call. Returns one vector per input, in order."""
    resp = await oai.embeddings.create(model=model, input=inputs)
    return [d.embedding for d in resp.data]


async def build_index(
    *,
    repo: Repo,
    oai: AsyncOpenAI,
    chat_ids: list[int],
    model: str = DEFAULT_EMBED_MODEL,
    progress_cb=None,
) -> int:
    """Embed every message in `chat_ids` that doesn't yet have a row for `model`.

    Idempotent: re-running adds only what's missing. Returns the number of
    new rows written. `progress_cb(done, total)` is called between batches
    if provided (used by the CLI wrapper to show a Rich Progress bar).
    """
    total_written = 0
    for chat_id in chat_ids:
        missing = await repo.msg_ids_missing_embedding(chat_id, model)
        if not missing:
            continue
        # Pull the actual bodies for the missing msg_ids — cheap re-read,
        # avoids a column-oriented scan path through `iter_messages`.
        rows_to_write: list[tuple[int, int, str, bytes]] = []
        # Iterate in fixed batches; missing list is already sorted.
        for batch_start in range(0, len(missing), _EMBED_BATCH):
            batch_ids = missing[batch_start : batch_start + _EMBED_BATCH]
            # Range query: contiguous msg_ids form one slice. Batch is small.
            messages = await repo.iter_messages(
                chat_id, min_msg_id=min(batch_ids) - 1, max_msg_id=max(batch_ids)
            )
            # Filter to the exact set we asked for (range may pick up rows
            # that already have embeddings; we re-skip those).
            wanted = set(batch_ids)
            inputs: list[str] = []
            input_msgs: list[Message] = []
            for m in messages:
                if m.msg_id not in wanted:
                    continue
                body = _body_for_embedding(m)
                if not body:
                    continue
                inputs.append(body[:8000])  # API hard input limit per item
                input_msgs.append(m)
            if not inputs:
                continue
            try:
                vectors = await _embed_batch(oai, model, inputs)
            except Exception as e:
                log.warning(
                    "embeddings.batch_failed",
                    chat_id=chat_id,
                    batch=batch_start,
                    err=str(e)[:200],
                )
                continue
            for m, vec in zip(input_msgs, vectors, strict=False):
                rows_to_write.append((m.chat_id, m.msg_id, model, _vec_to_bytes(vec)))
            if progress_cb:
                progress_cb(batch_start + len(batch_ids), len(missing))
        written = await repo.put_embeddings(rows_to_write)
        total_written += written
    return total_written


async def semantic_search(
    *,
    repo: Repo,
    oai: AsyncOpenAI,
    question: str,
    chat_ids: list[int],
    model: str = DEFAULT_EMBED_MODEL,
    limit: int = 200,
) -> list[tuple[Message, float]]:
    """Embed `question`, cosine-rank stored vectors, return top-`limit` (msg, score).

    Score is cosine similarity in [-1, 1]; messages with no body / not yet
    indexed are simply absent. Returns `[]` if the chat has no embeddings
    yet — caller surfaces "run --build-index first".
    """
    rows = await repo.get_embeddings(chat_ids, model)
    if not rows:
        return []

    # Embed the question.
    qvec_list = (await _embed_batch(oai, model, [question]))[0]
    # numpy is the only place numpy.argsort would be tempting, but stdlib
    # is fine and avoids forcing numpy as a hard dep on the ask path.
    import math as _math

    qvec = qvec_list
    qnorm = _math.sqrt(sum(x * x for x in qvec)) or 1.0

    scored: list[tuple[int, int, float]] = []  # (chat_id, msg_id, score)
    for chat_id, msg_id, vec_bytes in rows:
        v = _bytes_to_vec(vec_bytes)
        # Cosine similarity. Vectors from text-embedding-3-* are NOT
        # pre-normalized, so divide by norms.
        dot = 0.0
        vnorm_sq = 0.0
        for a, b in zip(qvec, v, strict=False):
            dot += a * b
            vnorm_sq += b * b
        if vnorm_sq == 0:
            continue
        score = dot / (qnorm * _math.sqrt(vnorm_sq))
        scored.append((chat_id, msg_id, score))

    scored.sort(key=lambda r: -r[2])
    top = scored[:limit]
    if not top:
        return []

    # Hydrate Message objects for the top hits. One iter_messages call per
    # chat (range-bounded) keeps it cheap.
    by_chat: dict[int, set[int]] = {}
    for cid, mid, _ in top:
        by_chat.setdefault(cid, set()).add(mid)
    msg_index: dict[tuple[int, int], Message] = {}
    for cid, ids in by_chat.items():
        msgs = await repo.iter_messages(cid, min_msg_id=min(ids) - 1, max_msg_id=max(ids))
        for m in msgs:
            if m.msg_id in ids:
                msg_index[(cid, m.msg_id)] = m
    out: list[tuple[Message, float]] = []
    for cid, mid, score in top:
        m = msg_index.get((cid, mid))
        if m is not None:
            out.append((m, score))
    return out


def default_model() -> str:
    """The model name written into `message_embeddings.model`.

    Lets a future config option override; today it's a constant. Callers
    use this when building / querying so both paths agree on which rows
    to look at.
    """
    return DEFAULT_EMBED_MODEL
