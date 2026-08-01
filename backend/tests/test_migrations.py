"""
Unit tests for startup schema migrations.

Regression: on a FRESH database, ``run_startup_migrations()`` must create the
full users table (``USERS_TABLE_DDL`` — including ``terms_accepted`` /
``terms_accepted_at`` referenced by the register INSERT) BEFORE the
notifications table, which ``REFERENCES users(id)``. Previously
``NOTIFICATIONS_TABLE_DDL`` ran first and failed on a brand-new DB (no users
table yet), skipping the whole migration block — the register endpoint then
lazily created a stale, minimal users table and its INSERT referencing
``terms_accepted`` threw, returning 500 "Registration failed".
"""
import asyncio
import inspect
import re
from unittest.mock import AsyncMock


class TestStartupMigrations:
    """run_startup_migrations() must be complete and correctly ordered on a fresh DB."""

    def _run_migrations(self, mock_pool, monkeypatch):
        """Execute run_startup_migrations() against the mocked pool (fresh per test)."""
        import app.main

        # main.py binds get_db_pool at import time (`from .database import ...`),
        # so the route-module patch in conftest doesn't reach it — patch it here.
        monkeypatch.setattr(app.main, "get_db_pool", AsyncMock(return_value=mock_pool))
        return asyncio.run(app.main.run_startup_migrations())

    def test_creates_users_before_notifications(self, mock_pool, mock_conn, monkeypatch):
        """Regression: USERS_TABLE_DDL must execute before NOTIFICATIONS_TABLE_DDL
        (which REFERENCES users) so a fresh DB doesn't skip the migration block."""
        self._run_migrations(mock_pool, monkeypatch)

        executed = [call.args[0] for call in mock_conn.execute.call_args_list]
        users_idx = next(
            i for i, sql in enumerate(executed) if "CREATE TABLE IF NOT EXISTS users" in sql
        )
        notif_idx = next(
            i for i, sql in enumerate(executed)
            if "CREATE TABLE IF NOT EXISTS notifications" in sql
        )
        assert users_idx < notif_idx, (
            "users must be created before notifications (which REFERENCES users) "
            "— otherwise a fresh DB skips the whole migration block"
        )

    def test_users_ddl_covers_register_insert_columns(self):
        """Regression: every column the register INSERT references must exist in
        USERS_TABLE_DDL, or a fresh DB returns 500 on registration."""
        from app.database import USERS_TABLE_DDL

        # Extract the INSERT's column list from the route source so this assertion
        # tracks the actual INSERT instead of a copied list.
        import app.routes.auth

        src = inspect.getsource(app.routes.auth)
        insert_match = re.search(
            r"INSERT INTO users\s*\((.*?)\)\s*VALUES", src, re.DOTALL
        )
        assert insert_match, "could not locate the users INSERT in auth.py"
        insert_cols = {c.strip() for c in insert_match.group(1).split(",")}

        assert insert_cols, "no columns parsed from the users INSERT"
        for col in insert_cols:
            assert re.search(rf"\b{re.escape(col)}\b", USERS_TABLE_DDL), (
                f"USERS_TABLE_DDL is missing column '{col}' referenced by the "
                f"register INSERT — a fresh DB would 500 on registration"
            )

    def test_migrations_idempotent_on_existing_schema(self, mock_pool, mock_conn, monkeypatch):
        """Running migrations twice must not raise (safe on existing DBs)."""
        self._run_migrations(mock_pool, monkeypatch)
        self._run_migrations(mock_pool, monkeypatch)  # second run must not raise

        executed = [call.args[0] for call in mock_conn.execute.call_args_list]
        users_runs = sum("CREATE TABLE IF NOT EXISTS users" in s for s in executed)
        assert users_runs == 2  # both runs executed the users DDL
