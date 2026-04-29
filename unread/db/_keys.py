"""Shared allowlists for the on-disk SQLite key/value tables.

Two tables in `data.sqlite` are key/value-shaped and need to refuse rows
outside a controlled set:

* ``secrets`` — credentials. Surface outside this allowlist either lets a
  typo silently store a never-read value, or (worse) lets a manual
  ``sqlite INSERT`` smuggle in a credential that the read overlay then
  trusts.
* ``app_settings`` — config overrides applied at startup. Surface outside
  this allowlist gets persisted but ignored, which is confusing.

Both lists used to be duplicated in :mod:`unread.secrets` and
:mod:`unread.db.repo`. They drifted at least once. Defining them here —
in a tiny module with no other imports — gives every reader (the wizard,
`put_secrets`, the legacy session-DB fallback, the sync bootstrap path,
the unit tests) a single source of truth.
"""

from __future__ import annotations

# Credentials persisted in `data.sqlite::secrets`. Each provider's chat
# key is stored separately; OpenAI's key additionally backs Whisper /
# embeddings / vision regardless of which provider drives chat (those
# capabilities have no non-OpenAI fallback in unread today).
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "telegram.api_id",
        "telegram.api_hash",
        "openai.api_key",
        "openrouter.api_key",
        "anthropic.api_key",
        "google.api_key",
    }
)


# Config overrides allowed in `data.sqlite::app_settings`. Used by the
# bootstrap-time `apply_db_overrides_sync` and the per-connect
# `_apply_db_overrides`. Adding a new key requires three matching edits;
# see the docstring in :data:`unread.db.repo._OVERRIDE_KEYS` for the
# full checklist.
OVERRIDE_KEYS: tuple[str, ...] = (
    # Languages
    "locale.language",
    "locale.content_language",
    "openai.audio_language",
    # Models
    "openai.chat_model_default",
    "openai.filter_model_default",
    "openai.audio_model_default",
    "enrich.vision_model",
    # Chat-provider routing (multi-provider support)
    "ai.provider",
    "ai.base_url",
    "ai.chat_model",
    "ai.filter_model",
    "local.base_url",
    # Enrichment defaults (booleans persisted as "0"/"1")
    "enrich.voice",
    "enrich.videonote",
    "enrich.video",
    "enrich.image",
    "enrich.doc",
    "enrich.link",
    # Analysis tuning
    "analyze.high_impact_reactions",
    "analyze.dedupe_forwards",
    "analyze.min_msg_chars",
    "analyze.plain_citations",
)
