"""F-6: money-moving admin endpoints must not accept long-lived API keys.

The finding: ``/api/ai/v1/admin/{refunds,api-keys,kill-switch,verification}``
depend on ``require_admin`` → ``get_current_user_id``, which accepts a ``tcdn_``
CDN API key. An admin's long-lived key — the credential most likely to sit in a
deployment's environment, a CI variable or a script — therefore carried its
owner's authority to mint credits, create AI keys and flip the payout kill
switch, while the routes and the checklist documented "admin JWT".

These tests pin three things about the fix (``require_admin_jwt``):

* a valid, admin-owning ``tcdn_`` API key is refused on every money endpoint,
  and refused *before* any admin lookup, so the key cannot even probe admin
  state;
* the intended caller — an admin session JWT — is still accepted (a control, so
  the refusal above cannot be a blanket rejection);
* the admin check itself is intact: a non-admin JWT is still 403.

The CDN payout routes that move money or bank details are held to the same rule:
they are in the same class of endpoint even though the audit named only the AI
namespace.
"""

from __future__ import annotations

import pytest

from app.main import app

#: A syntactically valid API key. The mocked DB resolves it to an admin, which
#: is the point: the refusal must come from the credential type, not from the
#: database saying "not an admin".
API_KEY = "tcdn_" + "A" * 43
API_KEY_HEADERS = {"Authorization": f"Bearer {API_KEY}"}

AI_ROUTES: list[tuple[str, str, dict | None]] = [
    (
        "POST",
        "/api/ai/v1/admin/refunds",
        {
            "account_id": "11111111-1111-4111-8111-111111111111",
            "amount_micro": 1_000,
            "reason": "api key should not reach this",
            "reference": "f6-1",
        },
    ),
    ("GET", "/api/ai/v1/admin/kill-switch", None),
    ("POST", "/api/ai/v1/admin/kill-switch", {"paused": True, "reason": "f6 test"}),
    ("POST", "/api/ai/v1/admin/verification", {"job_id": "job_f6", "outputs": {}}),
]

PAYOUT_MONEY_ROUTES: list[tuple[str, str, dict | None]] = [
    (
        "POST",
        "/billing/payouts/recipients",
        {"account_number": "254700000000", "name": "F6 Test"},
    ),
    ("DELETE", "/billing/payouts/recipients/RCP_f6", None),
    ("POST", "/billing/payouts", {"recipient_code": "RCP_f6", "amount": 100}),
]

MONEY_ROUTES = AI_ROUTES + PAYOUT_MONEY_ROUTES

#: The same routes as the app declares them (templates, not concrete ids), for
#: the structural sweep. Kept next to the request lists so a new money route is
#: added to both in one place.
MONEY_TEMPLATES: list[tuple[str, str]] = [
    (method, path) for method, path, _body in AI_ROUTES
] + [
    ("POST", "/billing/payouts/recipients"),
    ("DELETE", "/billing/payouts/recipients/{recipient_code}"),
    ("POST", "/billing/payouts"),
]


def _ai_surface_enabled() -> bool:
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the routes are not mounted
        return False


AI_OFF = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted",
)


@pytest.fixture()
def ai_admin(client, ai_env):
    """Point the AI admin surface at the in-memory coordinator, then clean up."""
    from app.ai import routes as ai_routes

    if not _ai_surface_enabled():
        yield None
        return
    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(ai_env.coordinator)
    try:
        yield ai_env
    finally:
        ai_routes.set_coordinator_for_tests(None)
        ai_routes.reset_for_tests()


@pytest.mark.parametrize("method,path,body", MONEY_ROUTES)
def test_an_admin_api_key_is_refused_and_never_reaches_the_admin_lookup(
    client, mock_conn, admin_user_id, method, path, body
):
    if path.startswith("/api/ai/") and not _ai_surface_enabled():
        pytest.skip("ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted")
    # The mocked DB would happily answer "this key's owner is an admin".
    mock_conn.fetchrow.return_value = {"user_id": admin_user_id, "is_admin": True}
    response = client.request(method, path, json=body, headers=API_KEY_HEADERS)
    assert response.status_code == 403, response.text
    assert "session token" in response.json()["detail"]

    # The refusal is made from the credential, before any DB work: an API key
    # cannot be used as an admin oracle.
    queries = [str(call.args[0]) for call in mock_conn.fetchrow.call_args_list]
    assert not any("is_admin" in query for query in queries), queries


@AI_OFF
@pytest.mark.parametrize("method,path,body", AI_ROUTES)
def test_an_ai_admin_key_is_refused_too(
    client, ai_admin, mock_conn, admin_user_id, method, path, body
):
    """An admin API key of any kind is the wrong credential for money."""
    mock_conn.fetchrow.return_value = {"user_id": admin_user_id, "is_admin": True}
    response = client.request(method, path, json=body, headers=API_KEY_HEADERS)
    assert response.status_code == 403, response.text


def test_an_admin_session_still_reaches_the_admin_routes(
    client, mock_conn, admin_headers
):
    """Control: the JWT-only rule must not have closed the intended caller.

    Uses the read-only recipient list (no Paystack round trip): the assertion is
    about which credential is accepted, not about the provider."""
    mock_conn.fetchrow.return_value = {"is_admin": True}
    mock_conn.fetch.return_value = []
    response = client.get("/billing/payouts/recipients", headers=admin_headers)
    assert response.status_code == 200, response.text


@AI_OFF
def test_an_admin_session_reaches_the_ai_kill_switch(
    client, ai_admin, mock_conn, admin_headers
):
    """The same control on the AI side (the surface the audit named)."""
    mock_conn.fetchrow.return_value = {"is_admin": True}
    response = client.get("/api/ai/v1/admin/kill-switch", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert "paused" in response.json()


def test_a_non_admin_session_is_still_refused(client, mock_conn, auth_headers):
    """Control: the admin check itself is unchanged — a plain user is 403."""
    mock_conn.fetchrow.return_value = {"is_admin": False}
    mock_conn.fetch.return_value = []
    response = client.get("/billing/payouts/recipients", headers=auth_headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"

    response = client.post(
        "/billing/payouts",
        json={"recipient_code": "RCP_x", "amount": 100},
        headers=auth_headers,
    )
    assert response.status_code == 403


def test_no_money_route_is_left_on_the_api_key_capable_dependency():
    """Structural sweep: every money route's dependency tree contains the
    JWT-only guard, so a route added later cannot quietly keep the old one."""

    def _names(dependant) -> set[str]:
        names: set[str] = set()
        stack = [dependant]
        while stack:
            node = stack.pop()
            for dependency in node.dependencies:
                names.add(getattr(dependency.call, "__name__", ""))
                stack.append(dependency)
        return names

    from fastapi.routing import APIRoute

    from app.ai import routes as ai_routes

    def _flatten(routes):
        found = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif getattr(route, "original_router", None) is not None:
                found.extend(_flatten(route.original_router.routes))
        return found

    import re

    def _shape(path: str) -> str:
        """Path templates compare by shape: /x/{code} and /x/RCP_1 are the same
        route, and the sweep must not depend on a concrete id."""
        return re.sub(r"\{[^}]+\}", "*", path)

    # (method, path): the same path can hold both a read (require_admin) and a
    # money write (require_admin_jwt), and only the write is in scope.
    expected = {(method, _shape(path)) for method, path in MONEY_TEMPLATES}
    seen: set[tuple[str, str]] = set()
    candidates = _flatten(app.routes) + list(ai_routes.admin_router.routes)
    for route in candidates:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            key = (method, _shape(route.path))
            if key not in expected:
                continue
            seen.add(key)
            assert "require_admin_jwt" in _names(route.dependant), (method, route.path)
    # The sweep runs in both flag positions: the AI admin routes are decorated on
    # the imported router whether or not it is mounted, so the dependency is
    # checked even in a flag-off deployment.
    missing = expected - seen
    assert not missing, f"money routes not found in the app: {missing}"
    assert seen, "no money route was inspected — the sweep found nothing"
