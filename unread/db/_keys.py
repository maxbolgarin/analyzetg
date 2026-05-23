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
        # Telethon `StringSession.save()` payload, written ONLY when the
        # passphrase backend is active. Replaces the on-disk
        # `session.sqlite` so encrypted-mode users have no plaintext
        # session blob to leak. Empty when the backend is `db` or
        # `keychain`; the read path in `tg/client.py` falls back to
        # the SQLiteSession file in those cases.
        "telegram.session_string",
    }
)


# Config overrides allowed in `data.sqlite::app_settings`. Used by the
# bootstrap-time `apply_db_overrides_sync` and the per-connect
# `_apply_db_overrides`. Adding a new key requires three matching edits;
# see the docstring in :data:`unread.db.repo._OVERRIDE_KEYS` for the
# full checklist.
OVERRIDE_KEYS: tuple[str, ...] = (
    # Languages — three independent axes; see :class:`unread.config.LocaleCfg`.
    "locale.language",
    "locale.report_language",
    "locale.content_language",
    "openai.audio_language",
    # Models
    "openai.chat_model_default",
    "openai.filter_model_default",
    "openai.audio_model_default",
    "enrich.vision_model",
    # AI routing — per-slot (provider, model). Each slot is independent:
    # analyze + filter + audio + vision can each pick its own provider.
    # `ai.provider` is the legacy umbrella key — kept on the allowlist
    # for one cycle so a fresh install can still read a row written by
    # an older binary; `db.repo._migrate_legacy_ai_provider` copies its
    # value into the four `*_provider` keys at bootstrap and deletes
    # the row. Stop writing it from new code.
    "ai.provider",
    "ai.base_url",
    "ai.chat_provider",
    "ai.chat_model",
    "ai.filter_provider",
    "ai.filter_model",
    "ai.audio_provider",
    "ai.audio_model",
    "ai.vision_provider",
    "ai.vision_model",
    "local.base_url",
    # Enrichment defaults (booleans persisted as "0"/"1")
    "enrich.voice",
    "enrich.videonote",
    "enrich.video",
    "enrich.image",
    "enrich.doc",
    "enrich.link",
    # Per-run soft cost caps on the two enrichment kinds that fan out
    # widely on busy chats (link + vision). 0 disables that enrichment;
    # any positive int bounds the number of msgs the orchestrator
    # processes before logging `enrich.cap_skip` and moving on. The cap
    # counts messages, not unique URLs / images.
    "enrich.max_link_fetches_per_run",
    "enrich.max_images_per_run",
    # Analysis tuning
    "analyze.high_impact_reactions",
    "analyze.dedupe_forwards",
    "analyze.min_msg_chars",
    "analyze.plain_citations",
    "analyze.no_citations",
    # Internal — DB schema version stamp written by `Repo.open` so a
    # downgrade can detect a future-version DB and refuse cleanly
    # rather than crashing with obscure column-mismatch errors.
    "_meta.schema_version",
    # Active secrets backend: "db" (default), "keychain", or "passphrase".
    # Selects which store `unread.secrets.read_secrets` consults at
    # startup. Persisted in `app_settings`, NOT `secrets`, since the
    # choice itself isn't sensitive — only the values it points at are.
    "secrets.backend",
    # Per-install KDF salt for the passphrase backend. 16 random bytes,
    # base64. Public — defense relies on the passphrase, not on the
    # salt being secret. Lets `unread security unlock` derive the key
    # without first reading any ciphertext.
    "security.kdf_salt",
    # Console verbosity. See :class:`unread.config.LoggingCfg.mode`.
    # Persisted choice is overridden at runtime by `UNREAD_LOG_MODE=…`
    # or the CLI flags (`-q`, `-v`, `--debug`).
    "logging.mode",
    # Wizard ergonomics. See :class:`unread.config.InteractiveCfg`.
    "interactive.offer_more_presets",
)
