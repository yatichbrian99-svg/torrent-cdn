"""Real-Postgres tests for the AI money paths (skipped without a test database).

The in-memory store cannot prove anything about concurrent writes; these tests
exist because ``PostgresStore.place_hold`` and ``PostgresStore.credit`` are the
only place where two requests can race. Set ``TEST_POSTGRES_URL`` to run them:

    TEST_POSTGRES_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \\
      .venv/bin/python -m pytest tests/test_ai_postgres.py -q

Each test uses fresh UUID accounts and removes its rows afterwards, so the suite
is safe to re-run against the same development database.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio

from app.ai.errors import InsufficientCredits
from app.ai.ledger import account_credits
from app.ai.store import AI_TABLES_DDL, PostgresStore

DSN = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_URL not set (real Postgres integration lane)"
)


@pytest_asyncio.fixture
async def store():
    from app.database import USERS_TABLE_DDL

    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=24)
    async with pool.acquire() as conn:
        # ``ai_accounts`` references ``users``; the real app creates users first.
        await conn.execute(USERS_TABLE_DDL)
        await conn.execute(AI_TABLES_DDL)
    settings = type("S", (), {"environment": "development"})()
    yield PostgresStore(pool, settings)
    await pool.close()


async def _cleanup(store: PostgresStore, account_id: str, source: str) -> None:
    async with store.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM ai_ledger_entries WHERE credit_account = $1 OR debit_account = $2",
            account_credits(account_id),
            source,
        )
        await conn.execute("DELETE FROM ai_holds WHERE account_id = $1", uuid.UUID(account_id))


@pytest.mark.asyncio
async def test_concurrent_credits_with_the_same_key_post_once(store):
    account = str(uuid.uuid4())
    source = "gateway:test"
    try:
        await asyncio.gather(
            *(
                store.credit(
                    account,
                    1_000,
                    source=source,
                    idempotency_key="same-webhook-event",
                    kind="credit_purchase",
                )
                for _ in range(40)
            )
        )
        assert await store.balance_micro(account) == 1_000

        await asyncio.gather(
            *(
                store.credit(
                    account,
                    1_000,
                    source=source,
                    idempotency_key=f"event-{i}",
                    kind="credit_purchase",
                )
                for i in range(20)
            )
        )
        assert await store.balance_micro(account) == 21_000
    finally:
        await _cleanup(store, account, source)


@pytest.mark.asyncio
async def test_concurrent_holds_cannot_overspend(store):
    account = str(uuid.uuid4())
    source = "gateway:test"
    jobs = [f"job-{account[:8]}-{i}" for i in range(50)]
    try:
        await store.credit(
            account,
            1_000,
            source=source,
            idempotency_key="hold-funding",
            kind="credit_purchase",
        )
        results = await asyncio.gather(
            *(store.place_hold(job, account, 100) for job in jobs),
            return_exceptions=True,
        )
        ok = [r for r in results if r is None]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(ok) == 10, f"{len(ok)} holds succeeded for a 10-hold balance"
        assert len(failures) == 40
        assert all(isinstance(f, InsufficientCredits) for f in failures)

        assert await store.held_micro(account) == 1_000
        assert await store.balance_micro(account) == 1_000
        # Available credit is exactly zero, never negative.
        assert await store.balance_micro(account) - await store.held_micro(account) == 0
    finally:
        await _cleanup(store, account, source)


@pytest.mark.asyncio
async def test_release_is_idempotent_and_restores_capacity(store):
    account = str(uuid.uuid4())
    source = "gateway:test"
    job = f"job-{account[:8]}-release"
    try:
        await store.credit(
            account, 500, source=source, idempotency_key="release-funding", kind="credit_purchase"
        )
        await store.place_hold(job, account, 500)
        with pytest.raises(InsufficientCredits):
            await store.place_hold(f"{job}-2", account, 1)

        await store.release_hold(job)
        await store.release_hold(job)  # replay: no double release, no error
        assert await store.held_micro(account) == 0
        await store.place_hold(f"{job}-3", account, 500)
        assert await store.held_micro(account) == 500
    finally:
        await _cleanup(store, account, source)


@pytest.mark.asyncio
async def test_every_ledger_entry_has_equal_debits_and_credits(store):
    """Global invariant: the ledger is double-entry, so it sums to zero."""
    account = str(uuid.uuid4())
    source = "gateway:test"
    try:
        await store.credit(
            account, 3_000, source=source, idempotency_key="inv-1", kind="credit_purchase"
        )
        await store.credit(
            account, 2_000, source=source, idempotency_key="inv-2", kind="credit_purchase"
        )
        label = account_credits(account)
        async with store.pool.acquire() as conn:
            credited, debited, self_dealing = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(CASE WHEN credit_account = $1 THEN amount_micro END), 0)
                           AS credited,
                       COALESCE(SUM(CASE WHEN debit_account = $1 THEN amount_micro END), 0)
                           AS debited,
                       COUNT(*) FILTER (WHERE debit_account = credit_account) AS self_dealing
                FROM ai_ledger_entries
                WHERE $1 IN (debit_account, credit_account)
                """,
                label,
            )
        # Every posting against the account must have a real counterparty and a
        # positive amount, and the balance must equal credits minus debits.
        assert int(credited) == 5_000
        assert int(debited) == 0
        assert int(self_dealing) == 0
        assert await store.balance_micro(account) == int(credited) - int(debited)
    finally:
        await _cleanup(store, account, source)


@pytest.mark.asyncio
async def test_startup_migrations_bootstrap_a_fresh_database():
    """A brand-new database must get the AI schema on first boot.

    Regression: the startup batch used to run ``ALTER TABLE uploads`` before
    ``uploads`` existed (it was created lazily by the first signup), so the whole
    batch aborted and every AI table was missing on a fresh checkout.
    """
    import uuid as _uuid

    import asyncpg as _asyncpg

    from app import main as app_main

    admin = await _asyncpg.connect(DSN)
    name = f"mig_check_{_uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    fresh_dsn = DSN.rsplit("/", 1)[0] + "/" + name
    pool = await _asyncpg.create_pool(fresh_dsn, min_size=1, max_size=2)
    try:
        async def fake_get_db_pool(*, settings=None):
            return pool

        original = app_main.get_db_pool
        app_main.get_db_pool = fake_get_db_pool
        try:
            await app_main.run_startup_migrations()
        finally:
            app_main.get_db_pool = original

        async with pool.acquire() as conn:
            tables = {
                row["tablename"]
                for row in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
        for required in ("users", "uploads", "analytics", "ai_accounts", "ai_jobs", "ai_ledger_entries"):
            assert required in tables, f"{required} missing after startup migrations"
    finally:
        await pool.close()
        admin = await _asyncpg.connect(DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_job_created_at_survives_the_postgres_round_trip(store):
    """Regression: a job read back from Postgres had created_at=0.

    That made every job look older than the TTL, so submit_receipt rejected
    every worker receipt as "job has expired" and nothing could ever settle.
    """
    import time as _time
    import uuid as _uuid

    from app.ai.store import JobRecord, Split

    account = str(_uuid.uuid4())
    job = JobRecord(
        job_id=_uuid.uuid4().hex,
        account_id=account,
        node_id="node_created_at_test",
        model="m",
        manifest_hash="a" * 64,
        status="placed",
        tier="standard",
        confidentiality="none",
        max_tokens=8,
        max_price_micro=100,
        prompt_tokens_estimate=4,
        hold_micro=100,
        ticket_hash="b" * 64,
        price_effective_from=0,
        split=Split(compute_pct=70, seeder_pct=20, platform_pct=10),
        created_at=int(_time.time()),
    )
    try:
        await store.create_job(job)
        loaded = await store.get_job(job.job_id)
        assert loaded is not None
        assert abs(int(loaded.created_at) - int(_time.time())) < 120
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM ai_jobs WHERE job_id = $1", job.job_id)


@pytest.mark.asyncio
async def test_idempotency_reservation_is_atomic_across_connections(store):
    """Row 2.5 on the real store: one claim, one replay, one refusal.

    The in-memory test proves the service logic; this proves the SQL the
    production store actually issues — ``INSERT ... ON CONFLICT DO NOTHING``
    is what makes a retry safe across coordinator processes.
    """
    account = str(uuid.uuid4())
    key = "postgres-idempotency"
    scope = "placement"
    body_hash = "c" * 64
    try:
        # Concurrent claims: exactly one wins, and it is the only one that gets
        # a None back ("you may proceed").
        claims = await asyncio.gather(
            *(store.reserve_idempotency(account, scope, key, body_hash) for _ in range(8))
        )
        assert sum(claim is None for claim in claims) == 1, claims

        await store.complete_idempotency(
            account, scope, key, {"job_id": "job_replayed"}
        )
        replayed = await store.reserve_idempotency(account, scope, key, body_hash)
        assert replayed == {
            "request_hash": body_hash,
            "response": {"job_id": "job_replayed"},
        }

        # A different body under the same key is visible to the service, which
        # turns it into a 409 rather than a replayed ticket.
        changed = await store.reserve_idempotency(account, scope, key, "d" * 64)
        assert changed is not None and changed["request_hash"] == body_hash

        await store.clear_idempotency(account, scope, key)
        assert await store.reserve_idempotency(account, scope, key, body_hash) is None
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ai_idempotency WHERE account_id = $1",
                uuid.UUID(account),
            )


@pytest.mark.asyncio
async def test_expire_stale_jobs_releases_the_hold(store):
    """A crashed worker must not lock the customer's credits forever."""
    import time as _time
    import uuid as _uuid

    from app.ai.store import JobRecord, Split

    account = str(_uuid.uuid4())
    source = f"gateway:expire-{_uuid.uuid4()}"
    job = JobRecord(
        job_id=_uuid.uuid4().hex,
        account_id=account,
        node_id="node_expiry_test",
        model="m",
        manifest_hash="a" * 64,
        status="placed",
        tier="standard",
        confidentiality="none",
        max_tokens=8,
        max_price_micro=100,
        prompt_tokens_estimate=4,
        hold_micro=100,
        ticket_hash="b" * 64,
        price_effective_from=0,
        split=Split(compute_pct=70, seeder_pct=20, platform_pct=10),
        created_at=int(_time.time()),
    )
    try:
        await store.credit(
            account, 1_000, source=source, idempotency_key="expire-seed", kind="credit_purchase"
        )
        await store.create_job(job)
        await store.place_hold(job.job_id, account, 100)
        assert await store.held_micro(account) == 100
        # TTL 0 makes the just-created job immediately stale.
        expired = await store.expire_stale_jobs(0)
        assert job.job_id in expired
        assert await store.held_micro(account) == 0
        reloaded = await store.get_job(job.job_id)
        assert reloaded is not None and reloaded.status == "expired"
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute("DELETE FROM ai_jobs WHERE job_id = $1", job.job_id)
            await conn.execute("DELETE FROM ai_holds WHERE job_id = $1", job.job_id)
            await conn.execute(
                "DELETE FROM ai_ledger_entries WHERE credit_account = $1 OR debit_account = $2",
                account_credits(account),
                source,
            )


@pytest.mark.asyncio
async def test_signup_grants_enforce_one_identity_and_count_velocity(store):
    """The anti-farming columns are real on Postgres, not only in the memory store.

    A second account with the same canonical email hash must not get a row (the
    partial unique index), and the velocity counters must respect their window
    and their grouping columns. This is the lane where "one grant per identity"
    is an index rather than a Python `if`.
    """
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    identity = "a" * 64
    try:
        assert await store.record_signup_grant(
            first,
            "ip-one",
            1_000,
            email_hash=identity,
            device_hash="device-one",
            email_domain="farm.example",
        )
        # Same inbox, different account/IP/device: the identity index refuses.
        assert not await store.record_signup_grant(
            second,
            "ip-two",
            2_000,
            email_hash=identity,
            device_hash="device-two",
            email_domain="farm.example",
        )
        assert await store.count_grants_for_identity(identity) == 1
        assert await store.count_grants_for_account(first) == 1
        assert await store.count_grants_for_account(second) == 0
        assert await store.count_grants_for_device("device-one", 0) == 1
        assert await store.count_grants_for_domain("farm.example", 0) == 1
        # A window that starts after the grant sees nothing.
        assert await store.count_grants_for_ip("ip-one", 1_001) == 0
        assert await store.count_signup_grants_since(2_001) == 0
    finally:
        async with store.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ai_signup_grants WHERE account_id = ANY($1::uuid[])",
                [uuid.UUID(first), uuid.UUID(second)],
            )
