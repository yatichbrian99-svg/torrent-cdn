"""Signup credit wiring: the seam between CDN verification and the coordinator.

``Coordinator.grant_signup_credits`` was fully tested and never called, so a
real signup received nothing. These tests pin the seam that fixed it: the flag
gate, the domain-separated IP hash, the "never raise into auth" contract, and
the one route that is cheap to drive directly.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.ai import signup
from app.ai.errors import ConflictError


class FakeCoordinator:
    def __init__(self, *, granted: int = 500_000_000, raises: Exception | None = None):
        self.calls: list[tuple[str, str]] = []
        self.granted = granted
        self.raises = raises

    async def grant_signup_credits(self, account_id: str, ip_hash: str) -> int:
        self.calls.append((account_id, ip_hash))
        if self.raises is not None:
            raise self.raises
        return self.granted


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("ENABLE_TCDN_AI", "true")


def install(monkeypatch, coordinator: FakeCoordinator) -> FakeCoordinator:
    async def fake_get_coordinator():
        return coordinator

    monkeypatch.setattr("app.ai.routes.get_coordinator", fake_get_coordinator)
    return coordinator


class TestGrantHelper:
    @pytest.mark.asyncio
    async def test_flag_off_never_builds_a_coordinator(self, monkeypatch):
        monkeypatch.delenv("ENABLE_TCDN_AI", raising=False)

        async def explode():
            raise AssertionError("the coordinator must not be constructed")

        monkeypatch.setattr("app.ai.routes.get_coordinator", explode)
        assert await signup.grant_signup_credit("user-1", "203.0.113.9") == 0

    @pytest.mark.asyncio
    async def test_grant_passes_a_domain_separated_ip_hash(self, monkeypatch, flag_on):
        coordinator = install(monkeypatch, FakeCoordinator())

        assert await signup.grant_signup_credit("user-1", "203.0.113.9") == 500_000_000
        assert len(coordinator.calls) == 1
        account_id, hashed = coordinator.calls[0]
        assert account_id == "user-1"
        assert hashed == signup.ip_hash("203.0.113.9")
        assert len(hashed) == 64 and set(hashed) <= set("0123456789abcdef")
        assert "203.0.113.9" not in hashed, "the IP itself must never be stored"

    def test_ip_hash_is_stable_and_network_keyed(self):
        assert signup.ip_hash("203.0.113.9") == signup.ip_hash("203.0.113.9")
        assert signup.ip_hash("203.0.113.9") != signup.ip_hash("203.0.113.10")
        # Case/whitespace differences must not create a second bucket.
        assert signup.ip_hash(" 203.0.113.9 ") == signup.ip_hash("203.0.113.9")
        # Unknown addresses share one bucket: the cap fires sooner, never later.
        assert signup.ip_hash(None) == signup.ip_hash("")

    @pytest.mark.asyncio
    async def test_capped_grant_is_reported_not_raised(self, monkeypatch, flag_on):
        install(
            monkeypatch,
            FakeCoordinator(raises=ConflictError("cap", code="signup_credit_limit")),
        )
        assert await signup.grant_signup_credit("user-1", "203.0.113.9") == 0

    @pytest.mark.asyncio
    async def test_duplicate_grant_returns_zero(self, monkeypatch, flag_on):
        install(monkeypatch, FakeCoordinator(granted=0))
        assert await signup.grant_signup_credit("user-1", "203.0.113.9") == 0

    @pytest.mark.asyncio
    async def test_a_broken_coordinator_never_breaks_signup(self, monkeypatch, flag_on):
        install(monkeypatch, FakeCoordinator(raises=RuntimeError("pool exhausted")))
        # Registration must not fail because the AI surface is unhealthy.
        assert await signup.grant_signup_credit("user-1", "203.0.113.9") == 0


class TestVerifyEmailRoute:
    @pytest.mark.asyncio
    async def test_verifying_an_address_issues_the_credit(self, monkeypatch):
        from app.routes import verification

        user_id = "11111111-1111-4111-8111-111111111111"
        granted: list[tuple[str, str]] = []

        async def fake_grant(account_id, ip):
            granted.append((str(account_id), ip))
            return 500_000_000

        class FakeConn:
            async def fetchrow(self, *_args, **_kwargs):
                return {"id": user_id, "verified": False}

            async def execute(self, *_args, **_kwargs):
                return "UPDATE 1"

        class FakePool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return FakeConn()

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        async def fake_pool():
            return FakePool()

        monkeypatch.setattr(
            verification, "decode_verification_token", lambda _t: user_id
        )
        monkeypatch.setattr(verification, "get_db_pool", fake_pool)
        monkeypatch.setattr(verification, "grant_signup_credit", fake_grant)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/verify-email",
                "headers": [],
                "client": ("203.0.113.9", 1234),
            }
        )
        from app.models import EmailVerifyRequest

        result = await verification.verify_email(
            EmailVerifyRequest(token="tok"), request
        )

        assert result["detail"] == "Email verified successfully"
        assert granted == [(user_id, "203.0.113.9")]

    @pytest.mark.asyncio
    async def test_an_already_verified_address_is_not_granted_twice(self, monkeypatch):
        from app.routes import verification

        user_id = "11111111-1111-4111-8111-111111111111"
        granted: list[str] = []

        async def fake_grant(account_id, _ip):
            granted.append(str(account_id))
            return 0

        class FakeConn:
            async def fetchrow(self, *_args, **_kwargs):
                return {"id": user_id, "verified": True}

        class FakePool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self_inner):
                        return FakeConn()

                    async def __aexit__(self_inner, *_exc):
                        return False

                return _Ctx()

        async def fake_pool():
            return FakePool()

        monkeypatch.setattr(
            verification, "decode_verification_token", lambda _t: user_id
        )
        monkeypatch.setattr(verification, "get_db_pool", fake_pool)
        monkeypatch.setattr(verification, "grant_signup_credit", fake_grant)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/verify-email",
                "headers": [],
                "client": ("203.0.113.9", 1234),
            }
        )
        from app.models import EmailVerifyRequest

        result = await verification.verify_email(
            EmailVerifyRequest(token="tok"), request
        )
        assert result["detail"] == "Email already verified"
        assert granted == []
