"""
Tests for the email-verification session gate (closes the register-token hole).

A freshly registered account used to receive a FULL session token that worked
before the email was verified — a user could register, get a token, and upload
files / mint API keys without ever proving the inbox. The login endpoint was
already gated on verification; the register-issued token was not.

The gate (`_enforce_email_verified` in app/auth.py) now restricts unverified
sessions to the verification allowlist (/auth/verification-status,
/auth/resend-verification, /auth/me) whenever `email_verification_required`
is on — the same flag login uses. These tests pin both sides: unverified
tokens are blocked from real endpoints, and the verification flow itself
keeps working. With the flag off (the default in dev/test) behaviour is
unchanged.
"""
from unittest.mock import AsyncMock

from app.auth import API_KEY_PREFIX


def _enable_verification(monkeypatch):
    """Flip email_verification_required on for one test (auto-restored)."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "email_verification_required", True)


class TestUnverifiedSessionGate:
    def test_unverified_token_blocked_from_files(self, client, auth_headers, mock_conn, monkeypatch):
        """The register-issued token of an unverified account must NOT reach
        a real endpoint — 403 before the handler runs."""
        _enable_verification(monkeypatch)
        mock_conn.fetchrow.return_value = {"verified": False}

        resp = client.get("/files", headers=auth_headers)
        assert resp.status_code == 403
        assert "Email not verified" in resp.json()["detail"]

    def test_unverified_token_blocked_even_with_verified_false(self, client, auth_headers, mock_conn, monkeypatch):
        """Same gate fires on another capability endpoint (billing), not just /files."""
        _enable_verification(monkeypatch)
        mock_conn.fetchrow.return_value = {"verified": False}

        resp = client.get("/billing/status", headers=auth_headers)
        assert resp.status_code == 403
        assert "Email not verified" in resp.json()["detail"]

    def test_verification_allowlist_reachable(self, client, auth_headers, mock_conn, monkeypatch):
        """The verification flow itself must keep working for unverified users."""
        _enable_verification(monkeypatch)
        # email included: the resend handler reads user["email"] from its row
        mock_conn.fetchrow.return_value = {"verified": False, "email": "test@example.com"}

        # status + identity + resend all allowed through the gate
        resp = client.get("/auth/verification-status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"verified": False}

        resp = client.get("/auth/me", headers=auth_headers)
        assert resp.status_code == 200

        resp = client.post("/auth/resend-verification", headers=auth_headers)
        assert resp.status_code == 200

    def test_verified_token_reaches_endpoints(self, client, auth_headers, mock_conn, monkeypatch):
        """Once verified, the same token works — the gate only restricts
        unverified accounts."""
        _enable_verification(monkeypatch)
        mock_conn.fetchrow.return_value = {"verified": True}
        mock_conn.fetch.return_value = []  # files list is empty

        resp = client.get("/files", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_unverified_api_key_rejected(self, client, mock_conn, monkeypatch, test_user_id):
        """An API key belonging to an unverified account is rejected too —
        otherwise minting a key pre-verification would bypass the gate."""
        _enable_verification(monkeypatch)
        token = API_KEY_PREFIX + "a" * 32
        # 1st fetchrow: api_keys lookup → owner; 2nd: the verified gate → False
        mock_conn.fetchrow.side_effect = [
            {"user_id": test_user_id},
            {"verified": False},
        ]

        resp = client.get("/files", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "Email not verified" in resp.json()["detail"]

    def test_gate_off_by_default_preserves_behaviour(self, client, auth_headers, mock_conn):
        """With email_verification_required off (dev/test default) the gate is
        a no-op — existing behaviour is unchanged."""
        mock_conn.fetchrow.return_value = {"verified": False}
        mock_conn.fetch.return_value = []

        resp = client.get("/files", headers=auth_headers)
        assert resp.status_code == 200


class TestAutoVerifyFallback:
    """When NO email service is configured, verification is impossible — the
    register and resend flows must mark the account verified rather than
    stranding it behind the gate forever. Keyed on the provider being ABSENT
    (is_email_service_configured() False), never on a flaky send."""

    def test_register_auto_verifies_when_no_email_service(self, client, mock_conn):
        """A new user is marked verified immediately when there's no provider.
        (Test env has no RESEND_API_KEY, so send_verification_email returns
        False and is_email_service_configured() is False — the natural state.)"""
        # H1 FIX: no pre-check SELECT — the INSERT is the first fetchrow.
        mock_conn.fetchrow.side_effect = [
            {"id": "11111111-1111-4111-8111-111111111111"},  # insert result
        ]

        resp = client.post("/auth/register", json={
            "email": "newuser@example.com",
            "password": "securePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 200
        # The register handler must have run the auto-verify UPDATE
        update_calls = [c for c in mock_conn.execute.call_args_list
                        if "verified = TRUE" in str(c)]
        assert update_calls, "expected register to auto-verify when no email service"

    def test_register_keeps_unverified_when_service_configured_but_flaky(self, client, mock_conn, monkeypatch):
        """A configured-but-failing provider must NOT auto-verify — the strict
        gate stays engaged and the user retries via resend."""
        import app.routes.auth as auth_route

        monkeypatch.setattr(auth_route, "is_email_service_configured", lambda: True)
        mock_conn.fetchrow.side_effect = [
            {"id": "11111111-1111-4111-8111-111111111111"},
        ]

        resp = client.post("/auth/register", json={
            "email": "flaky@example.com",
            "password": "securePass123",
            "terms_accepted": True,
        })
        assert resp.status_code == 200
        update_calls = [c for c in mock_conn.execute.call_args_list
                        if "verified = TRUE" in str(c)]
        assert not update_calls, "a configured-but-flaky provider must not auto-verify"

    def test_resend_auto_verifies_when_no_email_service(self, client, mock_conn, monkeypatch):
        """Resend with no provider marks the account verified (the response
        message is now honest) instead of lying about it.
        Uses a DISTINCT user id: the resend cooldown dict is process-global, so
        sharing test_user_id with the earlier allowlist test would 429 here."""
        from uuid import UUID

        from app.auth import create_access_token

        _enable_verification(monkeypatch)
        uid = UUID("33333333-3333-4333-8333-333333333333")
        headers = {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        mock_conn.fetchrow.return_value = {"verified": False, "email": "test@example.com"}

        resp = client.post("/auth/resend-verification", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == {"detail": "Email verified automatically (email service not configured)"}
        update_calls = [c for c in mock_conn.execute.call_args_list
                        if "verified = TRUE" in str(c)]
        assert update_calls, "resend must actually set verified when no email service"

    def test_resend_keeps_unverified_when_service_configured_but_flaky(self, client, mock_conn, monkeypatch):
        """Configured-but-failing provider: resend says the email could not be
        sent and does NOT verify — the gate stays engaged. (Distinct user id,
        same cooldown-dict reason as the auto-verify sibling.)"""
        from uuid import UUID

        import app.routes.verification as verification_route
        from app.auth import create_access_token

        _enable_verification(monkeypatch)
        uid = UUID("44444444-4444-4444-8444-444444444444")
        headers = {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        monkeypatch.setattr(verification_route, "is_email_service_configured", lambda: True)
        mock_conn.fetchrow.return_value = {"verified": False, "email": "test@example.com"}

        resp = client.post("/auth/resend-verification", headers=headers)
        assert resp.status_code == 200
        assert "could not be sent" in resp.json()["detail"]
        update_calls = [c for c in mock_conn.execute.call_args_list
                        if "verified = TRUE" in str(c)]
        assert not update_calls


class TestResendCooldownAndAlreadyVerified:
    """Resend endpoint edge cases: the 60s per-user cooldown and the
    already-verified short-circuit. Uses DISTINCT user ids because the cooldown
    dict is process-global (same isolation rule as the auto-verify siblings)."""

    def test_resend_second_call_within_60s_returns_429(self, client, mock_conn, monkeypatch):
        """First resend sends the email; an immediate second one is rate-limited
        with a 429 and a 'Please wait Ns' message. The cooldown check runs
        BEFORE the DB lookup, so it fires regardless of user state."""
        from uuid import UUID

        import app.routes.verification as verification_route
        from app.auth import create_access_token

        _enable_verification(monkeypatch)
        uid = UUID("55555555-5555-4555-8555-555555555555")
        headers = {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        mock_conn.fetchrow.return_value = {"verified": False, "email": "test@example.com"}
        # Deterministic send: succeeds, so the second call can only hit the
        # cooldown (no auto-verify / flaky-provider branch to reason about).
        monkeypatch.setattr(
            verification_route, "send_verification_email", AsyncMock(return_value=True)
        )

        first = client.post("/auth/resend-verification", headers=headers)
        assert first.status_code == 200
        assert first.json() == {"detail": "Verification email sent"}

        second = client.post("/auth/resend-verification", headers=headers)
        assert second.status_code == 429
        assert "Please wait" in second.json()["detail"]

    def test_resend_for_verified_user_returns_already_verified(self, client, mock_conn, monkeypatch):
        """An already-verified account gets 'Email already verified' (200) and
        NEVER records a cooldown (the verified check short-circuits before the
        cooldown write) — so even an immediate second call is not rate-limited
        and no email is sent."""
        from uuid import UUID

        import app.routes.verification as verification_route
        from app.auth import create_access_token

        _enable_verification(monkeypatch)
        uid = UUID("66666666-6666-4666-8666-666666666666")
        headers = {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        mock_conn.fetchrow.return_value = {"verified": True, "email": "test@example.com"}
        send = AsyncMock(return_value=True)
        monkeypatch.setattr(verification_route, "send_verification_email", send)

        first = client.post("/auth/resend-verification", headers=headers)
        assert first.status_code == 200
        assert first.json() == {"detail": "Email already verified"}

        # No cooldown was written for this user — the immediate second call is
        # also 'already verified', never a 429, and send was never invoked.
        second = client.post("/auth/resend-verification", headers=headers)
        assert second.status_code == 200
        assert second.json() == {"detail": "Email already verified"}
        send.assert_not_awaited()
