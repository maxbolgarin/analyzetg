# Pre-prod code review — `unread` (post-fix-pass: deferred items only)

**Status: ready to ship.** All 11 release blockers and the targeted HIGH / MEDIUM items have been resolved on `release/v1.0` (1314 tests passing, lint clean against `main`). What remains below is the post-1.0 polish backlog — defense-in-depth, non-user-facing edge cases, performance, and one or two behaviors worth documenting before someone rediscovers them.

This file is intentionally narrowed. The original review (full BLOCKERS / HIGH / MEDIUM / TEST GAPS lists, including everything fixed in the v1.0 pass) is preserved in git history at `a2349ae^..` if you need the full pre-fix snapshot.

---

## MEDIUM — worth fixing in a v1.1 maintenance pass

### Crypto / secrets

- **ChaCha20 envelope: salt + nonce framing is unauthenticated.** `crypto.py:153-163` — an attacker who can edit the on-disk envelope can swap the `salt` or `nonce` and the parser doesn't notice until the AEAD verify fires. Combined with the (now-fixed) auto-cache hole this would have allowed offline passphrase-guessing; with #1 closed the residual risk is "attacker who already has write to your DB can grind passphrase guesses without bumping a counter". Worth a `$u3$` envelope that HMACs the framing in a future pass.

### Sync / DB

- **`Repo` shares a single aiosqlite connection across coroutines.** `repo.py:73-78`. Two reads can interleave mid-batch with a write that hasn't committed yet. Today the orchestrator serializes well enough that this hasn't bitten, but a connection-per-task pool (or explicit `BEGIN IMMEDIATE` on every batch write) would make the invariant local rather than implicit.
- **`_apply_additive_migrations` ALTERs aren't wrapped in `BEGIN IMMEDIATE`.** Two fresh `unread` invocations against the same brand-new DB can race the first migration. Rare in practice (real installs migrate once at first run) but worth wrapping.

### Media / enrich

- **Whisper file-size cap hardcoded 24 MB; default transcribe model doesn't accept opus.** `media/download.py:19`, `enrich/audio.py`. `gpt-4o-mini-transcribe` (the default) rejects `.oga` opus voice notes with a 4xx — users hit it the moment they enrich a Telegram voice message with the default model. Either pre-transcode opus → mp3 in the audio path or document the model swap.
- **`extract_text` reads the whole file into memory.** `files/extractors.py:141`. Stdin cap is 100 MB; large local PDFs / docx files OOM the process. Stream the extractor.

### Export

- **`extra_json` built by string concatenation.** `enrich/document.py:250` — invalid JSON if `ext` contains `"` or `\`. `json.dumps` the dict instead.
- **CSV / JSONL output paths aren't validated to be under `reports_dir()` when implicit.** `export/`. A `--output ../../etc/passwd` slips through. Add a "must be under `reports_dir()` unless `--output` is explicit" guard.

### CLI / runtime

- **Mark-read uses `max(prior_pool.msg_id)`, depends on retrieval scoring.** `ask/commands.py` — re-running an `ask` against the same chat with a different ranker drifts the mark-read pointer because the "highest-id message in the cited pool" is implementation-dependent. Use `repo.get_max_msg_id` or document the behavior.
- **`_dispatch_analyze` has overlapping `--save` / `--no-save` / `--console` flags with no consistency check.** `cli.py:465`. Today silently collapses; explicit "you passed both" error would be clearer.
- **`providers.py:127` constructs a fresh provider just to read `default_chat_model`.** Instantiates the SDK client unnecessarily. Cheap fix: read the class attribute.
- **`_run` swallows `RuntimeError` / `typer.Exit` chains in `diagnostics.build_bug_report`.** `diagnostics.py:170` — the bug-report bundle aborts for the most-needed users (whose runs crashed). Bypass `_run` for the bundle path or add a `--unsafe-traceback` opt-in.
- **`_ensure_ready_for_analyze` can nest `asyncio.run` if any caller goes async.** `cli.py:1284`. Today only sync callers reach it; pin the invariant or add an `asyncio.get_running_loop()` guard.
- **`_print_config_status` does a sync sqlite read on every `--help` render.** `cli.py:828`. ~10 ms latency on `unread --help`. Cache the read or skip when `--help` is in argv.
- **`apply_db_overrides_sync` runs at import time; corrupt `data.sqlite` crashes `unread --help`.** `cli.py:146`. Smoke test in `tests/e2e/test_smoke_local.py` covers the fresh-install path; a corrupt-DB regression isn't covered. Defensive try/except + warning on the import-time read.
- **`_PreferSubcommandsGroup.parse_args` writes to private `ctx._protected_args`.** Click 8.3+ private API. Pin `click<9` in `pyproject.toml` until the override is validated against newer Click.

### Tests

- **Strengthen `assert ... is not None` smoke checks** in `tests/test_folders.py`, `tests/test_repo_*.py`, `tests/test_killme.py`. They confirm a row exists but not its content; a content regression would slip through. Low-priority polish.

---

## How to consume this file

This list is the post-1.0 backlog. Expected workflow:

1. After `v1.0.0` ships, open a tracking issue (or `docs/release/v1.1-followups.md`) seeded from this file.
2. Each item above maps to one PR-sized fix; group two or three per maintenance release.
3. None of the remaining items block a v1.0 ship — they were either already noted as MEDIUM (defense-in-depth) or surface only in narrow paths.

The full pre-fix-pass review (with the resolved BLOCKERS / HIGH list) is in git: `git show a2349ae:CODE_REVIEW.md`. The fix-pass commit log is on `release/v1.0`: `git log --oneline release/v1.0 ^main`.
