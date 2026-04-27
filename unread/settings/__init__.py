"""Persistent user settings stored in `app_settings` (SQLite).

`unread settings` overlays these on top of `config.toml` / defaults at every
`open_repo`, so a user can save their language preferences once instead
of editing config files. See `unread/db/repo.py:_apply_db_overrides`.
"""
