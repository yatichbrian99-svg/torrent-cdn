"""Schema contract: the SQL the coordinator ships must match ``AI_TABLES_DDL``.

Checklist row 1.9 proves the DDL block is idempotent and upgrade-safe. It cannot
prove the second half of "Models + ai_* DDL": that every statement the store
actually issues is valid *against that schema*. The failure mode is specific and
quiet — ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
exists, so adding a column to a query without the matching ``ALTER TABLE … ADD
COLUMN IF NOT EXISTS`` produces a schema that looks fine on a fresh database and
raises ``UndefinedColumnError`` in production on every upgraded one.

``PREPARE`` is the cheap, exact check: it plans the statement (validating every
table, column and cast) without executing it or touching a row. These tests run
it over every SQL literal in ``app/ai``, on a real Postgres, against a freshly
applied DDL block.

The negative control at the end matters as much as the positive ones: a scanner
that only ever says "fine" would pass on a schema with drift, so one test proves
``PREPARE`` rejects a column the schema never got.
"""

from __future__ import annotations

import ast
import os
import re
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.ai.store import AI_TABLES_DDL

DSN = os.environ.get("TEST_POSTGRES_URL")
requires_db = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_URL not set (real Postgres integration lane)"
)

AI_DIR = Path(__file__).resolve().parents[1] / "app" / "ai"
VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")
AI_TABLES = sorted(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", AI_TABLES_DDL))


def _sql_literals() -> list[tuple[str, int, str]]:
    """Every string literal in app/ai that starts a SQL statement on an ai_* table."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(AI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value
            upper = text.upper()
            if "ai_" not in text:
                continue
            if not re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b", upper):
                continue
            if not any(re.search(rf"\b{v}\b", upper) for v in VERBS):
                continue
            found.append((path.name, node.lineno, text))
    return found


@pytest_asyncio.fixture
async def schema():
    """A fresh, isolated schema with the real DDL applied (dropped on teardown)."""
    from app.database import USERS_TABLE_DDL

    name = f"schema_contract_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{name}"')
    pool = await asyncpg.create_pool(
        DSN, min_size=1, max_size=4, server_settings={"search_path": name}
    )
    try:
        async with pool.acquire() as conn:
            # ai_accounts references users(id); the real app creates users first.
            await conn.execute(USERS_TABLE_DDL)
            await conn.execute(AI_TABLES_DDL)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


@requires_db
@pytest.mark.asyncio
async def test_every_statement_is_valid_against_the_schema(schema):
    statements = _sql_literals()
    assert statements, "no SQL found in app/ai — the extractor or the layout changed"

    failures: list[str] = []
    checked = 0
    async with schema.acquire() as conn:
        for index, (name, lineno, sql) in enumerate(statements):
            # A dynamically formatted statement cannot be planned statically;
            # it is also a smell on a money path, so surface it rather than
            # silently skipping.
            assert "{" not in sql and "%s" not in sql, (
                f"{name}:{lineno} builds SQL dynamically — inline it so this "
                f"test can validate it"
            )
            try:
                await conn.execute(f"PREPARE contract_{index} AS {sql}")
                checked += 1
            except asyncpg.PostgresError as exc:
                failures.append(
                    f"{name}:{lineno}: {exc}\n    {sql.strip().splitlines()[0]}"
                )

    assert not failures, (
        f"{len(failures)} statement(s) do not match AI_TABLES_DDL:\n\n"
        + "\n".join(failures)
        + "\n\nIf a column is new, add `ALTER TABLE … ADD COLUMN IF NOT EXISTS` "
        "to the DDL block (CREATE TABLE IF NOT EXISTS will not do it)."
    )
    assert checked == len(statements)


@requires_db
@pytest.mark.asyncio
async def test_the_checker_rejects_a_column_the_schema_never_got(schema):
    """Negative control: without this, the test above could be a no-op."""
    async with schema.acquire() as conn:
        for sql in (
            "SELECT does_not_exist FROM ai_jobs",
            "SELECT * FROM ai_jobs WHERE nope = $1",
            "INSERT INTO ai_jobs (job_id, nope) VALUES ($1, $2)",
        ):
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(f"PREPARE drift_control AS {sql}")


@requires_db
@pytest.mark.asyncio
async def test_no_table_in_the_schema_is_dead(schema):
    """Every ai_* table must be queried somewhere, or it should not exist."""
    referenced = set(
        re.findall(r"\b(ai_\w+)\b", " ".join(s for _, _, s in _sql_literals()))
    )
    dead = sorted(set(AI_TABLES) - referenced)
    assert AI_TABLES, "DDL parsed as having no tables"
    assert not dead, (
        f"tables created by AI_TABLES_DDL but never queried: {dead}. "
        "Either wire them up or drop them from the schema."
    )


@requires_db
@pytest.mark.asyncio
async def test_every_table_has_a_primary_key(schema):
    """A table without a PK accepts duplicate rows; none of these are logs-only."""
    async with schema.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.tablename
            FROM pg_tables t
            WHERE t.schemaname = current_schema()
              AND t.tablename ~ '^ai_'
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_constraint c
                  WHERE c.conrelid =
                        (quote_ident(t.schemaname) || '.' || quote_ident(t.tablename))::regclass
                    AND c.contype = 'p'
              )
            ORDER BY 1
            """
        )
    missing = [row["tablename"] for row in rows]
    assert not missing, f"ai_* tables without a primary key: {missing}"
