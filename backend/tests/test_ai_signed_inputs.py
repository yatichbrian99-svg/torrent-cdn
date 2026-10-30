"""Hostile payloads on every signed route: never 5xx, never a side effect.

Why this file exists: ZAP counted **156 × 500** on ``POST /api/ai/v1/nodes/heartbeat``
(and a MEDIUM "Format String Error" on the same path). The cause was the inner
payload being unpacked inside the handler —

    payload = HeartbeatPayload(**envelope.payload)

— so pydantic's ``ValidationError`` (not an ``AIError``) escaped ``app/main.py``'s
error handler as a 500, and a non-string key would have raised ``TypeError``
before validation started. The fix is ``parse_signed_payload``: typed, bounded and
strict, as the first statement, so a hostile payload is a 422 with a stable
``invalid_payload`` code and nothing is read or written.

This is the property test that keeps it fixed. It drives the real routes through
the ASGI app, so a new signed route that forgets the helper fails here.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def ai_http(ai_env):
    """ASGI client over the AI router mounted explicitly.

    Deliberately **not** ``app.main.app``: the host app mounts /api/ai/v1 only
    when ``ENABLE_TCDN_AI`` is on, so a suite run with the flag off would answer
    404 for every route and this whole file would pass without touching a
    handler. Mounting the router here means the property under test — hostile
    payload in, clean 4xx out — is exercised in both flag states.
    """
    from contextlib import asynccontextmanager

    import httpx
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError

    from app.ai import routes as ai_routes
    from app.ai.errors import AIError
    from app.ai.routes import ai_error_handler
    from app.main import _validation_error_handler

    application = FastAPI()
    application.include_router(ai_routes.router)
    application.add_exception_handler(AIError, ai_error_handler)
    # The production handler, not FastAPI's default: the default echoes the
    # offending input (`"input": "<what was sent>"`) back to the caller, which is
    # both an information echo and, for a large field, a bandwidth amplifier.
    # Using the real one here means the no-echo assertion below fails if main.py's
    # generic handler is ever weakened.
    application.add_exception_handler(
        RequestValidationError, _validation_error_handler
    )

    @asynccontextmanager
    async def _client():
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        ai_routes.set_coordinator_for_tests(None)
        ai_routes.reset_for_tests()

    return _client

#: Present in some payloads so "the rejection echoes attacker input" is testable.
MARKER = "zap-hostile-8f2c1d"


def _envelope(payload) -> dict:
    return {
        "payload": payload,
        "key_id": "k" * 40,
        "alg": "Ed25519",
        "sig": "s" * 88,
    }


#: Shape mutations a fuzzer or an active scanner will produce: wrong types, huge
#: values, nesting, nulls, unicode/control characters, negative numbers, bools
#: where ints are expected, floats, unknown fields, and a payload that is not even
#: an object.
HOSTILE_PAYLOADS = [
    ("null", None),
    ("list", [1, 2, 3]),
    ("string", MARKER),
    ("empty-object", {}),
    ("unknown-field", {"node_id": "node_1", "sent_at": 1, "surprise": MARKER}),
    ("wrong-types", {"node_id": 12345, "sent_at": "not-an-int"}),
    ("negative", {"node_id": "node_1", "sent_at": -1, "queue_depth": -5}),
    ("huge", {"node_id": "n" * 5000, "sent_at": 10**60}),
    ("nested", {"node_id": {"a": {"b": ["c"]}}, "sent_at": [1, 2]}),
    ("unicode", {"node_id": "nöde\u0000" + MARKER, "sent_at": 1}),
    ("float", {"node_id": "node_1", "sent_at": 1.5}),
    ("bool-as-int", {"node_id": "node_1", "sent_at": True}),
    ("marker-value", {"node_id": MARKER, "sent_at": 1}),
]

#: Every route whose body is a ``SignedEnvelope``. (path, needs a worker API key)
SIGNED_ROUTES = [
    ("/api/ai/v1/nodes/heartbeat", False),
    ("/api/ai/v1/nodes/register", True),
    ("/api/ai/v1/attestations", True),
    ("/api/ai/v1/jobs/job_x/receipt", False),
]

PAYLOAD_IDS = [name for name, _ in HOSTILE_PAYLOADS]


class TestHostileSignedPayloads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,needs_key", SIGNED_ROUTES, ids=[r[0] for r in SIGNED_ROUTES])
    @pytest.mark.parametrize("payload", [p for _, p in HOSTILE_PAYLOADS], ids=PAYLOAD_IDS)
    async def test_never_a_5xx_and_never_a_state_change(
        self, ai_env, ai_http, path, needs_key, payload
    ):
        headers = {"Authorization": f"Bearer {ai_env.api_key}"} if needs_key else {}
        nodes = len(ai_env.store.nodes)
        jobs = len(ai_env.store.jobs)
        audit = len(ai_env.store.audit_log)

        async with ai_http() as client:
            response = await client.post(path, json=_envelope(payload), headers=headers)

        assert response.status_code < 500, (
            f"{path} answered {response.status_code} for a hostile payload: {response.text[:300]}"
        )
        assert response.status_code != 200, (
            f"{path} accepted a hostile payload: {response.text[:300]}"
        )
        # No rejection may echo what was sent — the ZAP alert was about input
        # reaching a handler, and an echo is how it reaches a client's logs.
        assert MARKER not in response.text
        assert len(ai_env.store.nodes) == nodes, f"{path} changed the node registry"
        assert len(ai_env.store.jobs) == jobs, f"{path} created or changed a job"
        assert len(ai_env.store.audit_log) == audit, f"{path} wrote an audit row"

    @pytest.mark.asyncio
    async def test_heartbeat_rejects_a_bad_inner_payload_with_a_stable_code(
        self, ai_env, ai_http
    ):
        """The exact shape that produced 500s: a parseable envelope, a bad payload."""
        async with ai_http() as client:
            response = await client.post(
                "/api/ai/v1/nodes/heartbeat",
                json=_envelope({"node_id": "node_1", "sent_at": "not-an-int"}),
            )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "invalid_payload"
        # Field paths and error types only — never the values (see errors.py).
        assert error["details"]["fields"]
        assert "not-an-int" not in response.text

    @pytest.mark.asyncio
    async def test_a_non_string_key_cannot_reach_the_handler(self, ai_env, ai_http):
        """`Model(**{"1": ...})` raised TypeError; JSON keys are strings, but the
        payload can hold objects whose members are not field names."""
        async with ai_http() as client:
            response = await client.post(
                "/api/ai/v1/nodes/heartbeat",
                json=_envelope({"node_id": "node_1", "sent_at": 1, "1": "x", "": "y"}),
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_a_valid_but_unknown_node_is_a_404_not_a_crash(
        self, ai_env, ai_http
    ):
        async with ai_http() as client:
            response = await client.post(
                "/api/ai/v1/nodes/heartbeat",
                json=_envelope({"node_id": "node_never_registered", "sent_at": 1}),
            )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_the_receipt_route_keeps_its_own_error_codes(self, ai_env, ai_http):
        """Receipts validate their payload *after* the signature (by design), so the
        rejection there is `invalid_receipt`, not `invalid_payload` — but it is
        still a 4xx with no state change."""
        async with ai_http() as client:
            response = await client.post(
                "/api/ai/v1/jobs/job_x/receipt",
                json=_envelope({"job_id": "job_x", "output_tokens": "many"}),
            )
        assert response.status_code < 500
        assert response.json()["error"]["code"] in {
            "invalid_receipt",
            "invalid_payload",
            "job_mismatch",
        }
