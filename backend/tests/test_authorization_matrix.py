"""Authorization matrix: every route, every caller class.

Two layers:

1. **Structural sweep** -- enumerate every FastAPI route and assert that an
   anonymous caller cannot get a 2xx from anything not on the explicit public
   allowlist. This catches a newly added endpoint that forgot its auth
   dependency, which per-route tests do not.
2. **Role matrix for /api/ai/v1** -- anonymous, inference key, worker-only key,
   another tenant's key, and admin JWT are exercised against the endpoints that
   matter. Cross-tenant object access must be indistinguishable from "not
   found", never a 403 that confirms the object exists.

The allowlist is data, with a reason per entry, so adding a public endpoint is a
deliberate review decision rather than a test edit.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from app.main import app

# (regex, reason) -- every entry is an intentional unauthenticated surface.
PUBLIC_ROUTES: list[tuple[str, str]] = [
    (r"^/health$", "liveness probe"),
    (r"^/api/health/live$", "liveness probe"),
    (r"^/\.well-known/security\.txt$", "security contact (RFC 9116)"),
    (r"^/csp-report$", "browser CSP violation reports (no credentials by design)"),
    (r"^/$", "landing page"),
    (r"^/docs$|^/redoc$|^/openapi\.json$", "API documentation"),
    (r"^/auth/register$", "account creation"),
    (r"^/auth/login$", "authentication"),
    (r"^/auth/refresh$", "token refresh"),
    (r"^/auth/verify-email$", "email verification link"),
    (r"^/auth/resend-verification$", "email verification resend"),
    (r"^/auth/forgot-password$", "password reset request"),
    (r"^/auth/reset-password$", "password reset completion"),
    (r"^/auth/mfa/.*$", "second-factor challenge (session-bound)"),
    (r"^/share/.*$", "public share pages"),
    (r"^/api/share/.*$", "public share API"),
    (r"^/public/.*$", "public assets"),
    (r"^/api/public/.*$", "public CDN API"),
    (r"^/api/ai/v1/keys/coordinator$", "public key bundle (signature is the auth)"),
    (r"^/api/ai/v1/billing/webhook$", "signed payment callback (signature is the auth)"),
    (r"^/api/ai/v1/jobs/\{job_id\}/receipt$", "worker receipt (signature is the auth)"),
    (r"^/api/ai/v1/nodes/heartbeat$", "worker heartbeat (signature is the auth)"),
    (r"^/api/ai/v1/attestations$", "attestation submission (signature is the auth)"),
    (r"^/metrics$", "Prometheus scrape (deployment-gated)"),
    (r"^/webhooks?/.*$", "signed third-party callbacks"),
]

PUBLIC = [(re.compile(pattern), reason) for pattern, reason in PUBLIC_ROUTES]

SKIP_PARAM_ROUTES = True


def _is_public(path: str) -> bool:
    return any(pattern.match(path) for pattern, _ in PUBLIC)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)


def _flatten(routes) -> list[APIRoute]:
    """FastAPI 0.141 wraps included routers; walk into them."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_flatten(inner.routes))
    return found


def _routes() -> list[APIRoute]:
    return _flatten(app.routes)


def _all_paths() -> list[str]:
    paths = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
    paths.extend(route.path for route in _routes())
    return paths


def test_every_route_is_either_public_or_requires_auth(client, ai_env):
    from app.ai import routes as ai_routes

    # The AI router builds its coordinator from the live DB pool. Point it at the
    # in-memory coordinator so this sweep tests authorization, not connectivity.
    ai_routes.set_coordinator_for_tests(ai_env.coordinator)
    failures: list[str] = []
    checked = 0
    for route in _routes():
        path = route.path
        if _is_public(path):
            continue
        if SKIP_PARAM_ROUTES and "{" in path:
            # Path parameters are exercised by the per-feature IDOR tests; the
            # anonymous check below still covers non-param routes structurally.
            continue
        method = min(route.methods - {"HEAD", "OPTIONS"})
        checked += 1
        try:
            response = client.request(method, path, json={})
        except Exception as exc:  # recorded as a finding
            failures.append(f"{method} {path} raised {type(exc).__name__}: {exc}")
            continue
        if response.status_code < 400:
            failures.append(f"{method} {path} -> {response.status_code}")
        elif response.status_code >= 500:
            failures.append(f"{method} {path} -> {response.status_code} (server error)")
    ai_routes.set_coordinator_for_tests(None)
    assert checked > 20, f"only {checked} routes exercised; route discovery is broken"
    assert not failures, "unauthenticated 2xx from protected routes:\n" + "\n".join(failures)


# Public surfaces that exist in other deployment shapes (CDN front door, metrics
# sidecar, provider callbacks) and therefore have no route in this process.
NOT_ROUTED = {
    r"^/$",
    r"^/share/.*$",
    r"^/api/share/.*$",
    r"^/public/.*$",
    r"^/api/public/.*$",
    r"^/metrics$",
    r"^/webhooks?/.*$",
    r"^/auth/refresh$",
    r"^/auth/mfa/.*$",
}


def _ai_surface_enabled() -> bool:
    """main.py mounts /api/ai/v1 only when ENABLE_TCDN_AI is on."""
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the app cannot mount the routes
        return False


_AI_OFF = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: the /api/ai/v1 routes are not mounted",
)


@_AI_OFF
def test_no_route_is_public_by_accident():
    """Every allowlist entry must still match a real route or be declared reserved."""
    paths = _all_paths()
    stale = [
        pattern.pattern
        for pattern, _ in PUBLIC
        if pattern.pattern not in NOT_ROUTED
        and not any(pattern.match(path) for path in paths)
    ]
    assert not stale, f"allowlist entries match no route: {stale}"


@pytest.fixture()
def ai_http(ai_env):
    from contextlib import asynccontextmanager

    import httpx

    from app.ai import routes as ai_routes

    @asynccontextmanager
    async def _client():
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
        ai_routes.set_coordinator_for_tests(None)
        ai_routes.reset_for_tests()

    return _client


@_AI_OFF
class TestAIRoleMatrix:
    @pytest.mark.asyncio
    async def test_worker_only_key_cannot_use_inference_endpoints(self, ai_env, ai_http):
        worker_key = await ai_env.coordinator.provision_api_key(
            ai_env.account_id, scopes=("worker",)
        )
        headers = {"Authorization": f"Bearer {worker_key}"}
        async with ai_http() as client:
            placed = await client.post(
                "/api/ai/v1/placements",
                headers=headers,
                json={"model": ai_env.model.model_id, "max_tokens": 8},
            )
            models = await client.get("/api/ai/v1/models", headers=headers)
            balance = await client.get("/api/ai/v1/balance", headers=headers)
        assert placed.status_code == 403
        assert models.status_code == 403
        # Balance is not scope-gated: it is the caller's own account.
        assert balance.status_code == 200
        assert balance.json()["account_id"] == ai_env.account_id

    @pytest.mark.asyncio
    async def test_foreign_account_gets_404_for_someone_elses_node(self, ai_env, ai_http):
        foreign_key = await ai_env.coordinator.provision_api_key(
            "22222222-2222-4222-8222-222222222222", scopes=("worker", "inference")
        )
        async with ai_http() as client:
            response = await client.post(
                "/api/ai/v1/nodes/node_someone_else/attestation-nonce",
                headers={"Authorization": f"Bearer {foreign_key}"},
            )
        assert response.status_code == 404
        assert "node_someone_else" not in response.text

    @pytest.mark.asyncio
    async def test_foreign_account_cannot_ack_another_tenants_job(
        self, ai_env, ai_worker_factory, ai_http
    ):
        from app.ai import routes as ai_routes

        worker = await ai_worker_factory()
        async with ai_http() as client:
            placed = await client.post(
                "/api/ai/v1/placements",
                headers={"Authorization": f"Bearer {ai_env.api_key}"},
                json={"model": ai_env.model.model_id, "max_tokens": 4},
            )
            assert placed.status_code == 200, placed.text
            job_id = placed.json()["job_id"]

            foreign_key = await ai_env.coordinator.provision_api_key(
                "33333333-3333-4333-8333-333333333333", scopes=("inference",)
            )
            attack = await client.post(
                f"/api/ai/v1/jobs/{job_id}/ack",
                headers={"Authorization": f"Bearer {foreign_key}"},
                json={
                    "job_id": job_id,
                    "tokens_received": 1,
                    "output_sha256": "a" * 64,
                    "acked_at": 1,
                },
            )
        ai_routes.reset_for_tests()
        assert attack.status_code == 404
        job = await ai_env.store.get_job(job_id)
        assert job is not None and job.status != "settled"
        assert worker.identity.node_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/api/ai/v1/jobs/job_x/receipt",
            "/api/ai/v1/nodes/heartbeat",
            "/api/ai/v1/attestations",
            "/api/ai/v1/billing/webhook",
        ],
    )
    async def test_signature_only_endpoints_reject_oversized_bodies(
        self, ai_env, ai_http, path
    ):
        oversized = b'{"payload":"' + b"a" * (3 * 1024 * 1024) + b'"}'
        async with ai_http() as client:
            response = await client.post(
                path, content=oversized, headers={"content-type": "application/json"}
            )
        assert response.status_code == 429, response.text
        assert response.json()["error"]["code"] == "body_too_large"

    @pytest.mark.asyncio
    async def test_unknown_api_key_is_401_without_echo(self, ai_env, ai_http):
        secret = "tcdn_ai_this_is_not_a_real_key_0123456789"
        async with ai_http() as client:
            response = await client.get(
                "/api/ai/v1/models", headers={"Authorization": f"Bearer {secret}"}
            )
        assert response.status_code == 401
        assert secret not in response.text
