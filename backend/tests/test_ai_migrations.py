"""Schema-upgrade proof for the ``ai_*`` tables (checklist row 1.9).

The documented design (``app/ai/store.py``) is a hand-written, idempotent DDL
block: ``CREATE TABLE IF NOT EXISTS`` for new tables and
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` for new columns, applied on every
startup. This module proves the parts of that claim that are mechanically
checkable, against a real Postgres, and pins the part that is *not* automatic
so nobody assumes it is:

PROVEN HERE
* a fresh database gets all 14 tables, and re-running the block is a no-op;
* a database from a PREVIOUS version (one that lacks tables added later)
  upgrades by re-running the block, with existing rows intact;
* the ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` primitive the design relies
  on is re-runnable — running it twice leaves exactly one column;
* dropping every ``ai_*`` table is reversible for the legacy schema: ``users``
  and its rows survive the CASCADE, and re-running the block restores the AI
  tables empty (the app is back to "AI enabled, no data", not broken);
* with the flag off the legacy app boots and serves traffic without the AI
  schema being involved at all.

NOT AUTOMATIC (pinned by test_missing_column_is_not_reconciled)
* ``CREATE TABLE IF NOT EXISTS`` does NOT add a missing column to an existing
  table. A future column must therefore ship as an explicit
  ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` line inside ``AI_TABLES_DDL``.
  No column has been added since the schema was first committed, so no
  deployment has ever depended on column reconciliation.

Each test runs in its own throwaway schema (``search_path``) in the test
database, so it cannot touch development rows, and the schema is dropped on
teardown. Set ``TEST_POSTGRES_URL`` to run:

    TEST_POSTGRES_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \\
      .venv/bin/python -m pytest tests/test_ai_migrations.py -q
"""

from __future__ import annotations

import os
import re
import uuid

import asyncpg
import pytest
import pytest_asyncio

from app.ai.store import AI_TABLES_DDL

DSN = os.environ.get("TEST_POSTGRES_URL")
requires_db = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_URL not set (real Postgres integration lane)"
)

# Parsed from the DDL block itself, so a table added to the block without being
# covered here fails the "fresh database" test instead of silently passing.
AI_TABLES = sorted(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", AI_TABLES_DDL))


@pytest_asyncio.fixture
async def db():
    """A pool whose ``search_path`` is an isolated, disposable schema."""
    from app.database import USERS_TABLE_DDL

    name = f"mig_proof_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{name}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, server_settings={"search_path": name}
    )
    try:
        async with pool.acquire() as conn:
            # ai_accounts references users(id); the real app creates users first.
            await conn.execute(USERS_TABLE_DDL)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def _tables(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    )
    return {row["tablename"] for row in rows}


async def _seed_account(conn) -> uuid.UUID:
    uid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, 'x')",
        uid,
        f"{uid}@migration.test",
    )
    await conn.execute("INSERT INTO ai_accounts (account_id) VALUES ($1)", uid)
    return uid


@requires_db
@pytest.mark.asyncio
async def test_fresh_database_creates_every_table_and_reruns_cleanly(db):
    async with db.acquire() as conn:
        assert AI_TABLES, "DDL block parsed as empty — the regex or the DDL changed"
        await conn.execute(AI_TABLES_DDL)
        assert set(AI_TABLES) <= await _tables(conn)

        # Re-running the block on an existing schema must be a no-op, not an
        # error — this is the whole upgrade mechanism.
        await conn.execute(AI_TABLES_DDL)


@requires_db
@pytest.mark.asyncio
async def test_upgrade_from_a_previous_version_keeps_rows(db):
    async with db.acquire() as conn:
        await conn.execute(AI_TABLES_DDL)
        uid = await _seed_account(conn)

        # A previous version of the product did not have these three tables.
        later = ["ai_payouts", "ai_model_prices", "ai_signup_grants"]
        await conn.execute(f"DROP TABLE {', '.join(later)} CASCADE")
        assert not set(later) & await _tables(conn)

        # Upgrading is just running the current block against the old database.
        await conn.execute(AI_TABLES_DDL)

        assert set(AI_TABLES) <= await _tables(conn)
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM ai_accounts WHERE account_id = $1", uid
            )
            == 1
        ), "the upgrade lost an existing row"


@requires_db
@pytest.mark.asyncio
async def test_additive_column_statement_is_reentrant(db):
    async with db.acquire() as conn:
        await conn.execute(AI_TABLES_DDL)
        # The primitive the documented design relies on for new columns.
        for _ in range(2):
            await conn.execute(
                "ALTER TABLE ai_audit_log ADD COLUMN IF NOT EXISTS migration_probe TEXT"
            )
        count = await conn.fetchval(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'ai_audit_log'
              AND column_name = 'migration_probe'
            """
        )
        assert count == 1


@requires_db
@pytest.mark.asyncio
async def test_missing_column_is_not_reconciled(db):
    """Pins the limitation: a dropped column is NOT restored by the DDL block.

    If this ever starts failing, someone added column reconciliation — update
    the module docstring and checklist row 1.9 instead of deleting this test.
    """
    async with db.acquire() as conn:
        await conn.execute(AI_TABLES_DDL)
        await conn.execute("ALTER TABLE ai_audit_log DROP COLUMN detail CASCADE")
        await conn.execute(AI_TABLES_DDL)
        restored = await conn.fetchval(
            """
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'ai_audit_log'
              AND column_name = 'detail'
            """
        )
        assert restored == 0


@requires_db
@pytest.mark.asyncio
async def test_dropping_every_ai_table_is_reversible_for_the_legacy_schema(db):
    async with db.acquire() as conn:
        await conn.execute(AI_TABLES_DDL)
        uid = await _seed_account(conn)

        await conn.execute(f"DROP TABLE {', '.join(AI_TABLES)} CASCADE")

        tables = await _tables(conn)
        assert not set(AI_TABLES) & tables, "an ai_* table survived its own DROP"
        assert "users" in tables, (
            "dropping the AI tables took the legacy schema with it"
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM users WHERE id = $1", uid) == 1
        ), "the legacy user row did not survive the AI teardown"

        # Re-enabling the product on that database must rebuild empty tables,
        # not fail and not resurrect stale rows.
        await conn.execute(AI_TABLES_DDL)
        assert set(AI_TABLES) <= await _tables(conn)
        assert await conn.fetchval("SELECT count(*) FROM ai_accounts") == 0


def test_legacy_app_does_not_need_the_ai_schema(client):
    """Flag off: the CDN app boots and serves without any ai_* table existing."""
    assert client.get("/health").status_code == 200
    assert not [p for p in client.app.openapi()["paths"] if "/ai/" in p]
