"""Schema version stamp lets a downgrade detect a future-version DB.

Without the stamp, a user who upgrades and later downgrades sees obscure
column-not-found errors at runtime when the older code reads a column
the new schema added. With the stamp, the older code refuses to open
the DB and tells the user to upgrade — clean failure mode.
"""

from __future__ import annotations

import pytest

from unread.db.repo import SCHEMA_VERSION, Repo, SchemaVersionError


@pytest.mark.asyncio
async def test_first_open_stamps_current_version(tmp_path):
    db = tmp_path / "data.sqlite"
    repo = await Repo.open(db)
    try:
        stamp = await repo.get_app_setting("_meta.schema_version")
    finally:
        await repo.close()
    assert stamp == str(SCHEMA_VERSION)


@pytest.mark.asyncio
async def test_reopen_with_matching_stamp_works(tmp_path):
    db = tmp_path / "data.sqlite"
    r1 = await Repo.open(db)
    await r1.close()
    # Second open: stamp already present, should not raise.
    r2 = await Repo.open(db)
    await r2.close()


@pytest.mark.asyncio
async def test_open_refuses_future_version_db(tmp_path):
    db = tmp_path / "data.sqlite"
    # First, open + close to create a v1-stamped DB
    r1 = await Repo.open(db)
    # Now overwrite the stamp with a future version directly
    await r1._conn.execute(
        "UPDATE app_settings SET value=? WHERE key=?",
        (str(SCHEMA_VERSION + 5), "_meta.schema_version"),
    )
    await r1._conn.commit()
    await r1.close()

    with pytest.raises(SchemaVersionError) as ei:
        await Repo.open(db)
    msg = str(ei.value)
    assert "newer" in msg.lower()
    assert "pip install -U unread" in msg


@pytest.mark.asyncio
async def test_open_upgrades_missing_stamp_silently(tmp_path):
    """An older DB without the stamp gets stamped on first open by new code."""
    db = tmp_path / "data.sqlite"
    r1 = await Repo.open(db)
    # Simulate a pre-stamping DB by deleting the stamp.
    await r1.delete_app_setting("_meta.schema_version")
    await r1.close()
    # Re-open: should stamp without raising.
    r2 = await Repo.open(db)
    try:
        stamp = await r2.get_app_setting("_meta.schema_version")
    finally:
        await r2.close()
    assert stamp == str(SCHEMA_VERSION)
