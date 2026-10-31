"""40018 (SQL Injection): binding proof for the routes the scanner named.

ZAP's active scanner raised **HIGH** ``40018 SQL Injection`` with **empty
``evidence``** on every one of its 8 instances across:

* ``POST /api/ai/v1/placements``        (params max_tokens, max_price_micro,
  prompt_tokens_estimate, region, vram_gb)
* ``POST /api/ai/v1/nodes/register``    (param key_id)
* ``POST /api/ai/v1/jobs/{job_id}/ack`` (param output_sha256)
* ``POST /api/live/heartbeat``          (param session_id)

An empty ``evidence`` field means ZAP matched no database error text and no
reflected payload — the rule fired on a response-shape difference. This test
attacks the claim from the only place a query can be built: the store. It
records **every SQL statement and argument** the store methods those routes
reach hand to asyncpg, and asserts

1. no payload ever appears inside the SQL text, and
2. the payload *does* arrive as a bound argument (so it was not silently
   dropped before reaching the driver either).

If either half regressed, a payload-shaped string would land in the statement
itself and this test would fail. The static twin of this check is
``scripts/sql_injection_audit.py`` (AST, whole ``backend/app`` tree, wired into
``security.yml``), whose own negative tests live in ``tests/test_sql_injection.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.ai.store import NodeRecord, PostgresStore

ACCOUNT = "11111111-1111-4111-8111-111111111111"
REAL_KEY = "a" * 64

#: Classic payload families the scanner (and the owner's brief) name: quote,
#: boolean, time-based, stacked, unicode, comment, null-byte, UNION.
PAYLOADS = [
    "John Doe' OR '1'='1' -- ",
    "1 AND 1=1",
    "x'; SELECT pg_sleep(5); --",
    "1; DROP TABLE ai_jobs; --",
    "x' UNION SELECT key_hash FROM ai_api_keys --",
    "\u2019 OR 1=1 --",
    "x\x00' OR 1=1 --",
    "1'--",
]

_STATEMENTS_AHEAD = 100


class _RecordingConn:
    """Records (sql, args) for every asyncpg call, then answers empty."""

    def __init__(self, sink: list[tuple[str, tuple]]) -> None:
        self.sink = sink

    async def execute(self, sql: str, *args):
        self.sink.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args):
        self.sink.append((sql, args))
        return []

    async def fetchrow(self, sql: str, *args):
        self.sink.append((sql, args))

    async def fetchval(self, sql: str, *args):
        self.sink.append((sql, args))
        return 0

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()


class _RecordingPool:
    def __init__(self) -> None:
        self.sql: list[tuple[str, tuple]] = []

    def acquire(self):
        conn = _RecordingConn(self.sql)

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def _node(payload: str) -> NodeRecord:
    return NodeRecord(
        node_id=payload,
        owner_id=ACCOUNT,
        identity_public_key=payload,
        identity_key_id=payload,
        session_public_key=payload,
        region=payload,
        endpoint=payload,
        gpu_model=payload,
    )


#: One callable per store method the three AI routes reach with a client- or
#: node-supplied string. ``place_hold`` looks its job up by id; the ack path
#: reads both the job and the client receipt; registration inserts the node.
CALLS = {
    "placements/job lookup": lambda store, p: store.get_job(p),
    "jobs/ack receipt lookup": lambda store, p: store.get_ack(p),
    # amount 0 so the balance check passes and the INSERT (which carries the
    # hostile job_id as $1) is actually reached — a non-zero amount aborts on
    # InsufficientCredits against an empty fake balance and would leave the
    # binding claim untested.
    "placements/hold": lambda store, p: store.place_hold(p, ACCOUNT, 0),
    "placements/idempotency": lambda store, p: store.reserve_idempotency(
        ACCOUNT, "place", p, REAL_KEY
    ),
    "nodes/register": lambda store, p: store.register_node(_node(p)),
}


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: repr(p)[:28])
@pytest.mark.parametrize("label", sorted(CALLS))
@pytest.mark.asyncio
async def test_payload_is_bound_never_a_placeholder_for_sql(label, payload):
    pool = _RecordingPool()
    store = PostgresStore(pool, MagicMock())
    try:
        await CALLS[label](store, payload)
    except Exception:
        # A hostile value may be rejected before the driver (e.g. ``_uuid``
        # refusing a non-UUID). That is a clean refusal; the SQL captured so far
        # is still what this test inspects.
        pass

    assert pool.sql, f"{label}: no SQL was issued, so the binding claim is untested"

    for sql, args in pool.sql:
        assert payload not in sql, (
            f"{label}: the payload was INTERPOLATED into SQL:\n{sql!r}\n"
            "SQL must be a literal with $1/$2 placeholders."
        )
        assert "$1" in sql, f"{label}: statement has no bound placeholders:\n{sql!r}"

    # Compare against repr() on both sides: repr escapes quotes, backslashes and
    # NUL, so an exact repr match is the only sound comparison for payloads that
    # contain those characters.
    values = " ".join(repr(a) for _sql, args in pool.sql for a in args)
    assert repr(payload) in values, (
        f"{label}: the payload never reached the driver as an argument either "
        f"(recorded args: {values[:200]})"
    )
