# Pre-prod code review — `unread`

**Verdict: DO NOT SHIP YET.** 11 release blockers, several with data-loss / credential-exposure consequences.

Scope: 5 parallel reviews of ~36k LOC across security/crypto, core runtime/CLI, AI provider abstraction, Telegram/DB sync, and ask/wizard + tests. Plus targeted spot-checks.

---

## BLOCKERS — fix before any tag

### Security / credentials

1. **Auto-cached master key on disk with no TTL.** `unread/security/commands.py:496-502` (`cmd_upgrade`) calls into the `cmd_unlock` cache writer with `ttl_seconds=None`, dropping a passphrase-derived key into `~/.unread/.runtime/key` *forever* (`crypto.py:250-274`). On macOS/Windows this isn't on tmpfs — it survives reboot and `~/` backups. Anyone reading the file gets every secret.
   **Fix:** never auto-cache after upgrade; require explicit `unlock`; default TTL ≤ 30 min; refuse `ttl=None` unless the path resolves under a real tmpfs.

2. **AEAD has no `associated_data` — silent slot swaps.** `crypto.py:127, 148, 172, 192` calls ChaCha20Poly1305 with `associated_data=None`. Swapping the ciphertext stored under `openai.api_key` into the `telegram.api_hash` row decrypts cleanly.
   **Fix:** bind the slot name (and salt+nonce) as AAD; bump envelope to `$u2$`; re-encrypt on next upgrade/rotate.

3. **Passphrase lingers in process memory; Rich tracebacks print locals.** `secrets.py:155-172` keeps the passphrase in `_PROCESS_PASSPHRASE` for the life of the process and `util/logging.py:95` enables `rich_tracebacks=True`, which prints local variables on any unhandled exception — bypassing the redactor that only walks top-level event-dict keys.
   **Fix:** derive key, then `_PROCESS_PASSPHRASE = None`; disable `rich_tracebacks` unless `UNREAD_VERBOSE` is set.

4. **`.env` loader has no permission/symlink guard.** `config.py:326-352` reads `~/.unread/.env` regardless of mode and follows symlinks.
   **Fix:** `O_NOFOLLOW` open + refuse `st_mode & 0o077`. Also strip `\r` (CRLF leaves `\r` in API keys → 401 + traceback prints the value).

### Path / data-loss

5. **`unread killme` will `rmtree` whatever `UNREAD_HOME` points to with no sanity check.** `killme.py:84` + `core/paths.py:33`. `UNREAD_HOME=/` or `UNREAD_HOME=$HOME` is catastrophic.
   **Fix:** refuse to operate when `unread_home()` resolves to `/`, `$HOME`, any first-level dir under `/`, or doesn't contain a recognizable install marker (`install.toml` or `data.sqlite`). Also `cwd=Path.home()` when invoking the binary uninstall subprocess (`killme.py:109`).

6. **Path traversal in saved Telegram filenames.** `media/commands.py:51-57` `_safe_filename_component` only replaces `/`/`\`, leaves NUL/control/RTL-override/Windows-reserved names. RTL spoof + colons on NTFS reach disk as alternate data streams.
   **Fix:** whitelist `[A-Za-z0-9._-]`, reject reserved names, cap length at 200.

7. **Partial-download cleanup is a no-op.** `media/commands.py:205-207` unlinks a `.partial` file the code never creates — Telethon writes straight to the final path. Ctrl-C leaves a truncated `{msg_id}.jpg`; `_existing_for_msg` then skips it forever.
   **Fix:** download to `dest.with_suffix(dest.suffix + ".part")`, atomic rename on success, cleanup `.part` on exception.

### AI / providers

8. **Tiktoken used for Claude / Gemini token counts.** `util/tokens.py:11-21` falls through to `o200k_base` for non-OpenAI models. Off by 10-40%. Combined with #9, the chunker silently truncates or 4xx's after you've already paid for the map pass.
   **Fix:** provider-aware counting (`anthropic.messages.count_tokens`, `google.genai.models.count_tokens`); bump safety margin ≥25% on non-OpenAI.

9. **`MODEL_CONTEXT` only knows OpenAI ids — every Claude/Gemini model falls to 128k.** `analyzer/chunker.py:15-40`. Opus 4.7 advertised at 1M ctx is treated as 128k → 6-8× more chunks than needed → 6-8× the cost. The unknown-model warning fires once per process.
   **Fix:** move context window into `ai/models.py:ModelInfo`, look up via `find_model()`.

10. **Anthropic / Google adapters bypass the user-visible retry layer.** `retry_on_429` only wraps the OpenAI path. Anthropic SDK retries silently (looks like a hung process); Google has its own inline loop with different semantics. The orchestrator's truncation-retry on a 429 from Anthropic crashes instead of retrying.
    **Fix:** lift retry into a provider-aware wrapper; always emit the yellow status line.

11. **Release pipeline publishes wheels without running tests.** `.github/workflows/release.yml` runs `semantic-release` → `uv build` → `uv publish` with **no `pytest`/`ruff` step and no dependency on `ci.yml`.** A red main still ships to PyPI.
    **Fix:** `needs: [test, lint]` via `workflow_call`, or inline `uv run pytest -q && uv run ruff check .` before `uv build`. Also: the version-bump step writes `pyproject.toml` *after* tagging without committing/pushing back, so the published wheel and the tag disagree.

---

## HIGH — fix this week

### Crypto / secrets

- `_set_active_backend_sync` + `_persist_upgrade` are three separate transactions; SIGKILL between them leaves an unreadable install (`security/commands.py:62-84, 472-528`). Single transaction + write-new-rows-then-flip-flag pattern.
- `cmd_recover` runs Scrypt N times on wrong passphrases and leaks partial decrypts before raising (`commands.py:914-924`).
- `_load_dotenv` at import time injects secrets into `os.environ`, which every subsequent `subprocess.run` inherits (`config.py:377`, `cli.py:3117`). Read into a local dict instead.
- Some keyring backend exceptions can carry the supplied key value; the redactor regex only matches OpenAI's older format. Log `type(e).__name__` only.

### Core runtime / CLI

- `i18n.t()` raises `KeyError` on missing keys; some `_tf("…")` calls run during help rendering at import → one missing key breaks `--help` (`i18n.py:1860`). Return a sentinel + warn.
- `_run` strips traceback type qualnames and there's no file logger — production crashes are unreproducible (`cli.py:238-253`, `util/logging.py:90`). `diagnostics.collect_log_tail` looks at `settings.logging_file_path`, a field that doesn't exist in `Settings`.
- `runner._cmd_run_flat` doesn't thread `language`/`content_language` into `prepare_chat_run` even though the args are accepted. Reports come back in default locale.
- `killme` reads `_cache_path` via private import (`# type: ignore[attr-defined]`); rename → cached key file silently survives `killme`. Promote to public API.
- `_seed_home_templates` copies `.env.example` with `shutil.copyfile` (mode 0o644 default), then `tighten`s. Brief world-readable window. Use `secret_write_text`.

### Sync / DB

- `VACUUM INTO '{target}'` uses string interpolation (`db/repo.py:1793`). Lock down the `target` path (no `'`, `\0`, newline; absolute under known root).
- No `PRAGMA busy_timeout`; concurrent `unread sync` + `unread ask` race the writer (`db/repo.py:73-78`). Add `busy_timeout=15000`.
- `RateLimiter` (`util/flood.py:129-144`) mutates a shared list from multiple coroutines without a lock → occasional flood.
- Stale comment / over-fetch after the recent sync fix: `tg/sync.py:289-330` ignores `from_msg_id` whenever `since_date` is set, so a `--last-days 90` on a chat with 1M-id `local_max` re-walks 90 days of already-stored messages. `ask/commands.py:976-1044`'s comment still claims "more restrictive wins" — pick whichever bound is tighter at the call site (need `local_max`'s date).
- `retry_on_flood` (`util/flood.py:38-78`) doesn't cap FloodWait sleep — a 24h limit silently blocks the run.
- No file-size cap before `client.download_media` (`media/download.py:80-86`) → 4 GB videos fill the user's disk.
- CSV formula injection (`export/markdown.py:73-128`). Prefix any cell whose first char is `=+-@\t\r` with `'`.

### AI / providers

- Truncation retry doubles `max_tokens` and re-bills the full prompt (`openai_client.py:203-233`). No user-visible warning. Add yellow notice + opt-out flag; compute `_MAX_RETRY_TOKENS` from per-model max-output (Gemini 2.5 Flash caps at ~8k; current 16k ceiling masks the issue).
- No prompt-injection mitigation. Telegram message bodies and fetched web pages flow verbatim into prompts. Add a hardening clause to `presets/<lang>/_base.md` and wrap untrusted bodies with a sentinel.
- Auth-error classifier flags Anthropic 403s (content policy) and Google 403s (quota) as "your key is bad" (`openai_client.py:136-150`).
- Single message exceeding `budget` is emitted as an oversized chunk that the provider rejects with "prompt is too long" (`chunker.py:110-115`). Split intra-message at sentence boundaries or truncate with a marker.
- Anthropic SDK `max_retries=settings.openai.max_retries` with no provider-side surfacing (`anthropic_provider.py:67-76`). Set `max_retries=0` and own retries.

### Wizard / UX / docs

- `unread ask tg ...` shortcut runs *before* `_validate_scope_args`, so `--chat`/`--global` are silently dropped (`ask/commands.py:260-279`).
- `_count_custom_range` builds naive datetimes (`interactive.py:976-977`) while `_period_to_db_filters` uses UTC. Confirm-screen counts skew by host TZ.
- README + `config.toml.example` reference `unread tg init` (9 occurrences) — that command no longer exists.

### Tests

- **Anthropic and Google adapters have no real-translation tests.** `tests/test_ai_providers.py` only checks dispatch. `_FakeProvider` in `test_openai_client.py` stubs one shape. Translation logic (`_split_system_and_messages`, `_convert_messages`, finish-reason mapping, usage parsing) is dead-untested. Stub `anthropic.AsyncAnthropic.messages.create` and `google.genai.Client.aio.models.generate_content` and assert on input/output mapping.

---

## MEDIUM (selected)

- ChaCha20 envelope: `parse_envelope` framing has unauthenticated salt+nonce → swap-then-derive lets an attacker offline-verify passphrase guesses, especially when paired with #1 (`crypto.py:153-163`).
- `cmd_status` doesn't check `data.sqlite`/session file modes separately from the home dir (`security/commands.py:124-133`).
- `_b64decode(..., validate=True)` everywhere; today silently accepts non-base64 (`crypto.py:88-90`).
- `KEYCHAIN_SERVICE = "unread"` shared across installs — two installs clobber each other; any other Python process can `keyring.get_password("unread", ...)` (`secrets_backend.py:38`). Namespace per install.
- `Repo` shares one aiosqlite connection across coroutines; reads can interleave mid-batch writes (`repo.py:73-78`).
- `iter_messages`, `untranscribed_media`, `cache_iter_full` `fetchall()` everything → 1M-message chats OOM (`repo.py:608-611, 892-895, 1767-1770`).
- `temperature` unconditionally forwarded; OpenAI reasoning models (gpt-5/o-series) reject any value ≠ 1 — and the default chat model is `gpt-5.4-mini` (`openai_provider.py:64`).
- Anthropic empty-`messages` defensive branch injects `{"role": "user", "content": ""}`, which Anthropic 400s (`anthropic_provider.py:91-92`).
- `resp.text` on Gemini raises on safety-blocked candidates (`google_provider.py:135`). Wrap in try/except.
- Whisper file-size cap hardcoded 24 MB; `gpt-4o-mini-transcribe` (the default!) doesn't accept opus → voice notes 4xx (`media/download.py:19`, `enrich/audio.py`).
- Mark-read uses `max(prior_pool.msg_id)`, which depends on retrieval scoring — re-running drifts. Use `repo.get_max_msg_id` or document.
- `_dispatch_analyze` has overlapping `--save`/`--no-save`/`--console` flags with no consistency check (`cli.py:465`).
- `extra_json` built by string concatenation — invalid JSON if `ext` contains `"` or `\` (`enrich/document.py:250`).
- CSV/JSONL output paths aren't validated to be under `reports_dir()` when implicit (`export/`).
- `_apply_additive_migrations` ALTERs aren't wrapped in `BEGIN IMMEDIATE`; concurrent fresh starts can collide.
- `extract_text` reads whole file into memory (`files/extractors.py:141`); stdin cap is 100 MB.
- `cli.watch` runs `subprocess.run` inside an `async` function (`cli.py:3117`) — blocks the loop.
- `google_provider.py:131` uses `assert resp is not None` — stripped under `python -O`. Replace with explicit `raise`.
- `providers.py:127` constructs a fresh provider just to read `default_chat_model`, instantiating the SDK client unnecessarily.
- `runner.py:593` uses deprecated naive `datetime.utcnow()`.
- `_run` swallows `RuntimeError`/typer.Exit chains in `diagnostics.build_bug_report` (`diagnostics.py:170`) — bug-report bundle aborts for the most-needed users.
- `config.load_settings` "Layer 4" silently ignores secret-DB read errors (`config.py:422-441`).
- `_ensure_ready_for_analyze` can nest `asyncio.run` if any caller goes async (`cli.py:1284`).
- `_print_config_status` does a sync sqlite read on every `--help` render (`cli.py:828`).
- `apply_db_overrides_sync` runs at import time; corrupt `data.sqlite` crashes `unread --help` (`cli.py:146`).
- `_PreferSubcommandsGroup.parse_args` writes to private `ctx._protected_args`. Pin `click<9` until validated.
- LOW: SCRYPT params `N=2**17` are at the low end for 2026; bump to `N=2**18`.
- LOW: `_redact_processor` doesn't recurse into nested dicts/lists (`logging.py:64-74`).

---

## Test gaps to close

- **No tests** for `unread/completion/`, `unread/export/`, `unread/core/`. Add at minimum: `_resolve_shell` with a mocked `shellingham`, `_completion_script` content assertions, `compute_window`/`parse_ymd`/`derive_internal_id`, and the markdown formatter.
- **No tests** for the two recent sync-bound fixes (466bf69, 82801a6). Add a fake `iter_messages` that records the kwargs and assert what's forwarded for `(forward, since_date, from_msg_id)` permutations.
- Strengthen `assert ... is not None` smoke checks in `tests/test_folders.py`, `test_repo_*.py`, `test_killme.py` — they confirm a row exists but not its content.
- `tests/test_ask_mark_read.py:80` passes by accident (largest msg_id happens to be at index 0). Reorder pool so a regression to `pool[-1].msg_id` would fail.
- `tests/conftest.py:31` leaks a tmpdir per session.
- CI doesn't matrix providers (only one adapter is exercised even with mocks).

---

## Suggested release plan

**Tonight (cannot ship without):** items 1–11 above.

**This week:** all HIGH items, especially the docs/README drift (`unread tg init`), the sync over-fetch regression, the silent Anthropic 429 retries, and the Anthropic/Google adapter test coverage.

**Before public announcement:** the MEDIUM list, plus a real provider-matrix CI, file-logging via `RotatingFileHandler` so user bug reports include tracebacks, and a `unread tg folders` / README walkthrough revalidation against the current CLI.

**Spot-confirmed:** `killme` against `UNREAD_HOME=/` is unguarded; `_load_dotenv` follows symlinks; no `shell=True`/`eval`/`os.system` anywhere; SQL placeholders are parameter-bound (the two f-string SQL hits are placeholder generation only). The five blockers most likely to bite real users in week one are #1, #5, #8+#9 together (silent over-spend on Claude/Gemini), and #11 (red main publishes to PyPI).
