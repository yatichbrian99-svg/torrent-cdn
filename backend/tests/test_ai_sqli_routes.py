"""40018 (SQL Injection): dynamic proof over **every** `/api/ai/v1` input route.

ZAP's rule 40018 is accepted (``docs/tcdn-ai/LEVEL1_EXCEPTIONS.md``, option (b))
on the strength of three things, and this file is the dynamic third:

1. the static AST audit (``scripts/sql_injection_audit.py``) rejects any SQL in
   ``backend/app`` built with an f-string, ``.format()``, ``%`` or concatenation;
2. the store-level binding proof (``tests/test_ai_sqli_binding.py``) shows the
   payloads arrive as bound arguments for the statements the named routes reach;
3. **this** test drives the payloads through the real HTTP surface of every
   input route with a valid credential and asserts the observable properties a
   real injection would break.

What is asserted for each request:

* **never 5xx** — an injection attempt is a refused request, not a crash;
* **no database error text** in the body (driver, SQLSTATE, "syntax error at or
  near", Postgres' own encoding/parse messages);
* **no state change for a refused request** — if the route answered 4xx, the
  store snapshot (jobs, holds, ledger, nodes, receipts, keys, audit, …) must be
  byte-identical afterwards. A 2xx/4xx that legitimately creates a job is not a
  mutation caused by the payload and is not asserted against;
* **no gross timing difference** — the slowest payload request must stay within
  a generous multiple of the route's own benign baseline, which is what a
  time-based payload (``pg_sleep``) would break. The bound is deliberately loose
  (in-process ASGI, in-memory store); the exact statement-level proof is #2, so
  this is a smoke bound, not a timing oracle.

The route list is **derived from the routers**, not hardcoded: a new input
endpoint is covered the moment it exists, and the count is printed so the
coverage claim in the docs can be re-measured rather than remembered.
"""

from __future__ import annotations

import copy
import time
from typing import Any

import pytest

from app.ai import routes as ai_routes

#: Classic payload families: quote/boolean, time-based, stacked, UNION, unicode,
#: NUL, comment, arithmetic. Same set as the binding test, on purpose.
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

#: Strings a database or driver would put in an error page. None of these may
#: appear in a response to an injection attempt.
DB_ERROR_TEXT = (
    "asyncpg",
    "postgres",
    "psycopg",
    "sqlstate",
    "syntax error at or near",
    "invalid byte sequence",
    "character not in repertoire",
    "unterminated quoted string",
    "relation \"",
    "traceback",
)

HTTP_METHODS = ("post", "put", "patch", "delete")


def _input_routes() -> list[tuple[str, str, bool]]:
    """(path, method, needs_admin) for every non-GET route on the AI routers."""
    found: list[tuple[str, str, bool]] = []
    for router, needs_admin in ((ai_routes.router, False), (ai_routes.admin_router, True)):
        for route in router.routes:
            methods = sorted(getattr(route, "methods", []) or [])
            for method in methods:
                if method.lower() in HTTP_METHODS:
                    found.append((route.path, method.lower(), needs_admin))
    return sorted(set(found))


ROUTES = _input_routes()


class TestRouteInventory:
    def test_every_input_route_is_covered(self):
        """The count is the coverage claim; it is printed, not asserted by hand."""
        print(f"input routes covered: {len(ROUTES)}")
        for path, method, needs_admin in ROUTES:
            print(f"  {method.upper():6s} {path}{'   (admin)' if needs_admin else ''}")
        assert len(ROUTES) >= 12, "the AI surface lost input routes — check the mount"

    def test_the_routes_the_scanner_named_are_in_the_set(self):
        """A gate whose list silently dropped a previously-named path would pass
        vacuously."""
        paths = {path for path, _m, _a in ROUTES}
        for named in (
            "/api/ai/v1/placements",
            "/api/ai/v1/nodes/register",
            "/api/ai/v1/jobs/{job_id}/ack",
            "/api/ai/v1/attestations",
        ):
            assert named in paths, named


def _schema_for(app, path: str, method: str) -> dict:
    spec = app.openapi()
    body = (spec["paths"].get(path, {}).get(method, {}) or {}).get("requestBody")
    if not body:
        return {}
    content = body.get("content") or {}
    schema = content.get("application/json", {}).get("schema") or {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec.get("components", {}).get("schemas", {}).get(name, {})
    return schema if isinstance(schema, dict) else {}


def _default_for(prop: dict) -> Any:
    if "$ref" in prop:
        return {}
    kind = prop.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if "anyOf" in prop:
        for option in prop["anyOf"]:
            if option.get("type") in ("integer", "number", "boolean", "array", "object"):
                return _default_for(option)
        return "x"
    return "x"


def _body(schema: dict, hostile_field: str | None, payload: str) -> dict:
    props = schema.get("properties") or {}
    body = {name: _default_for(prop) for name, prop in props.items()}
    if hostile_field is None:
        return body
    body[hostile_field] = payload
    return body


def _string_fields(schema: dict) -> list[str]:
    props = schema.get("properties") or {}
    return sorted(
        name
        for name, prop in props.items()
        if prop.get("type") == "string" or "anyOf" in prop
    )


def _snapshot(store) -> dict:
    """A comparable picture of everything a request could write.

    ``api_keys[*].last_used_at`` is normalised away: authenticating a request
    stamps it, so it changes on every call regardless of the body and would
    make the mutation check fail on the *baseline* request, not on a payload.
    Everything a payload could actually write — jobs, holds, ledger, receipts,
    nodes, idempotency rows, audit entries — stays in the comparison.
    """
    snap: dict[str, Any] = {}
    for name, value in vars(store).items():
        if isinstance(value, (dict, list, set)):
            snap[name] = copy.deepcopy(value)
    for record in (snap.get("api_keys") or {}).values():
        if hasattr(record, "last_used_at"):
            record.last_used_at = None
    ledger = getattr(store, "ledger", None)
    if ledger is not None and hasattr(ledger, "entries"):
        snap["ledger.entries"] = copy.deepcopy(ledger.entries)
    return snap


def _assert_clean(resp, route: str, payload: str) -> None:
    text = resp.text.lower()
    assert resp.status_code < 500, f"{route}: {resp.status_code} for payload {payload!r}"
    for marker in DB_ERROR_TEXT:
        assert marker not in text, (
            f"{route}: response carried database error text {marker!r} "
            f"for payload {payload!r}: {resp.text[:300]}"
        )


@pytest.fixture()
def ai_routes_live(ai_env):
    """Point the mounted app's AI routes at the in-memory coordinator."""
    ai_routes.set_coordinator_for_tests(ai_env.coordinator)
    yield ai_env
    ai_routes.set_coordinator_for_tests(None)
    ai_routes.reset_for_tests()


@pytest.mark.usefixtures("client")
class TestPayloadsOnEveryInputRoute:
    def _headers(self, env, needs_admin: bool, admin_headers) -> dict[str, str]:
        if needs_admin:
            return dict(admin_headers)
        return {"Authorization": f"Bearer {env.api_key}"}

    def test_every_route_refuses_injection_safely(
        self, ai_env, ai_routes_live, client, admin_headers
    ):
        from app.main import app

        covered = 0
        for path, method, needs_admin in ROUTES:
            schema = _schema_for(app, path, method)
            headers = self._headers(ai_env, needs_admin, admin_headers)
            url = path.replace("{job_id}", "job-does-not-exist").replace(
                "{node_id}", "node-does-not-exist"
            )

            # Benign baseline for the timing bound (same shape, no payload).
            baseline = 0.0
            for _ in range(3):
                started = time.perf_counter()
                client.request(method.upper(), url, json=_body(schema, None, ""), headers=headers)
                baseline = max(baseline, time.perf_counter() - started)

            fields = _string_fields(schema) or [None]
            for payload in PAYLOADS:
                for field in fields:
                    if field is None:
                        body: Any = {"payload": {"x": payload}, "sig": payload}
                    else:
                        body = _body(schema, field, payload)

                    before = _snapshot(ai_env.store)
                    started = time.perf_counter()
                    resp = client.request(method.upper(), url, json=body, headers=headers)
                    elapsed = time.perf_counter() - started

                    _assert_clean(resp, f"{method.upper()} {path}", payload)
                    if resp.status_code >= 400:
                        assert _snapshot(ai_env.store) == before, (
                            f"{method.upper()} {path}: a refused payload "
                            f"({payload!r} in {field!r}) mutated state"
                        )
                    # A time-based payload (pg_sleep(5)) costs seconds; the
                    # bound is deliberately loose so scheduling noise cannot
                    # red the suite, while still being far below that.
                    threshold = max(1.5, baseline * 5)
                    assert elapsed < threshold, (
                        f"{method.upper()} {path}: payload {payload!r} took "
                        f"{elapsed:.3f}s vs benign {baseline:.3f}s (bound "
                        f"{threshold:.3f}s) — time-based?"
                    )
                    covered += 1

        assert covered >= len(ROUTES) * len(PAYLOADS), covered
        print(f"dynamic injection requests issued: {covered} over {len(ROUTES)} routes")

    def test_a_signed_envelope_with_a_hostile_payload_is_refused_cleanly(
        self, ai_env, ai_routes_live, client
    ):
        """Every signed route takes `{payload: {...}, sig}`; a hostile *inner*
        payload must be a 4xx with a stable code, never a 5xx (the defect the
        156 × 500 on /nodes/heartbeat once was)."""
        signed = [
            "/api/ai/v1/nodes/register",
            "/api/ai/v1/nodes/heartbeat",
            "/api/ai/v1/attestations",
            "/api/ai/v1/jobs/job-does-not-exist/ack",
            "/api/ai/v1/jobs/job-does-not-exist/receipt",
        ]
        headers = {"Authorization": f"Bearer {ai_env.api_key}"}
        for path in signed:
            for payload in PAYLOADS:
                for inner in (
                    {"node_id": payload},
                    {"job_id": payload, "acked_at": payload},
                    {"__proto__": {"x": payload}},
                    payload,
                    [payload],
                    None,
                ):
                    resp = client.post(
                        path, json={"payload": inner, "sig": payload}, headers=headers
                    )
                    _assert_clean(resp, path, payload)
                    assert resp.status_code < 500, resp.text
                    if resp.status_code >= 400:
                        assert resp.status_code in (400, 401, 403, 404, 409, 422), resp.text


def _load_env_flag() -> bool:
    try:
        from app.ai.config import get_ai_settings
        from app.config import get_settings

        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:
        return False


if not _load_env_flag():
    pytest.skip(
        "ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted on app.main.app",
        allow_module_level=True,
    )

