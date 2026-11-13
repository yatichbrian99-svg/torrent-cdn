"""Logout without Redis: revocation must survive an absent cache.

Production runs without Redis. The logout endpoint wrote the ``token_block:``
key to Redis only, so with Redis absent it was a silent no-op: the JWT stayed
valid until it expired (up to 24h after the user logged out), and the
``get_current_user_id`` check failed open by design because it had nowhere else
to look.

The fix adds a Postgres table of revoked token ids (``revoked_tokens``),
indexed by expiry and cleaned on every write, consulted only when Redis is
unavailable — so the fast path is unchanged and the durable path closes the gap.
"""

from __future__ import annotations

import time
from uuid import UUID

from app.auth import create_access_token

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def _token_and_jti() -> tuple[str, str]:
    from app.auth import decode_access_token

    token = create_access_token({"sub": str(USER_ID)})
    payload = decode_access_token(token)
    assert payload is not None
    return token, str(payload["jti"])


def _statements(mock_conn) -> list[str]:
    return [call.args[0] for call in mock_conn.execute.await_args_list]


class TestLogoutWritesTheDurableRow:
    def test_logout_records_the_jti_when_redis_is_absent(
        self, client, mock_conn, monkeypatch
    ):
        """No Redis is the production posture; the row is what revokes."""
        import app.routes.auth as auth_routes

        monkeypatch.setattr(auth_routes, "get_redis", _none_redis)
        token, jti = _token_and_jti()

        response = client.post(
            "/auth/logout", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text

        sql = _statements(mock_conn)
        insert = next(s for s in sql if "INSERT INTO revoked_tokens" in s)
        assert insert
        assert mock_conn.execute.await_args_list[0].args[1] == jti

    def test_logout_cleans_expired_rows(self, client, mock_conn, monkeypatch):
        import app.routes.auth as auth_routes

        monkeypatch.setattr(auth_routes, "get_redis", _none_redis)
        token, _ = _token_and_jti()
        client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert any("DELETE FROM revoked_tokens" in s for s in _statements(mock_conn))

    def test_logout_still_succeeds_when_both_stores_fail(
        self, client, mock_conn, monkeypatch
    ):
        """A logout must never 500 because a store is down."""
        import app.routes.auth as auth_routes

        async def _broken_redis():
            raise RuntimeError("redis down")

        async def _broken_insert(*args, **kwargs):
            raise RuntimeError("postgres down")

        monkeypatch.setattr(auth_routes, "get_redis", _broken_redis)
        monkeypatch.setattr(
            auth_routes, "revoke_token_jti_db", _broken_insert
        )
        token, _ = _token_and_jti()
        response = client.post(
            "/auth/logout", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200


class TestTheCheckUsesTheDurableTable:
    def test_a_revoked_token_is_refused_when_redis_is_absent(
        self, client, mock_conn, monkeypatch
    ):
        """The gap this closes: logged out, Redis absent, token still accepted."""
        import app.auth

        async def _none_redis():
            return None

        async def _revoked(jti):
            return True

        monkeypatch.setattr(app.auth, "get_redis", _none_redis)
        monkeypatch.setattr(app.auth, "is_token_jti_revoked_in_db", _revoked)
        token, _ = _token_and_jti()

        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert "Session invalidated" in response.json()["detail"]

    def test_an_unrevoked_token_still_works_without_redis(
        self, client, mock_conn, monkeypatch
    ):
        """The fallback must not turn \"no Redis\" into \"no sessions\"."""
        import app.auth

        async def _none_redis():
            return None

        async def _not_revoked(jti):
            return False

        monkeypatch.setattr(app.auth, "get_redis", _none_redis)
        monkeypatch.setattr(app.auth, "is_token_jti_revoked_in_db", _not_revoked)
        mock_conn.fetchrow.return_value = {
            "id": USER_ID,
            "email": "user@example.test",
            "plan": "free",
            "verified": True,
            "is_admin": False,
            "created_at": None,
        }
        token, _ = _token_and_jti()
        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text

    def test_redis_hit_skips_the_database(
        self, client, mock_conn, monkeypatch
    ):
        """With Redis answering, the per-request path stays a single GET."""
        import app.auth

        calls: list[str] = []

        async def _redis():
            return _RedisAnswer("1")

        async def _db(jti):
            calls.append(jti)
            return False

        monkeypatch.setattr(app.auth, "get_redis", _redis)
        monkeypatch.setattr(app.auth, "is_token_jti_revoked_in_db", _db)
        token, _ = _token_and_jti()
        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401  # blocked by Redis
        assert calls == []

    def test_redis_error_falls_back_to_the_database(
        self, client, mock_conn, monkeypatch
    ):
        import app.auth

        async def _broken_redis():
            raise RuntimeError("redis down")

        async def _revoked(jti):
            return True

        monkeypatch.setattr(app.auth, "get_redis", _broken_redis)
        monkeypatch.setattr(app.auth, "is_token_jti_revoked_in_db", _revoked)
        token, _ = _token_and_jti()
        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401

    def test_both_stores_down_fails_open_with_expiry_as_the_backstop(
        self, client, mock_conn, monkeypatch
    ):
        """Availability beats perfect revocation when nothing can be read."""
        import app.auth

        async def _broken_redis():
            raise RuntimeError("redis down")

        async def _broken_db(jti):
            raise RuntimeError("postgres down")

        monkeypatch.setattr(app.auth, "get_redis", _broken_redis)
        monkeypatch.setattr(app.auth, "is_token_jti_revoked_in_db", _broken_db)
        mock_conn.fetchrow.return_value = {
            "id": USER_ID,
            "email": "user@example.test",
            "plan": "free",
            "verified": True,
            "is_admin": False,
            "created_at": None,
        }
        token, _ = _token_and_jti()
        response = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text


class _RedisAnswer:
    """Minimal async Redis stand-in for one GET."""

    def __init__(self, value: str | None):
        self._value = value

    async def get(self, key: str):
        return self._value

    async def set(self, *args, **kwargs):
        return True


async def _none_redis():
    return None


def test_tokens_carry_a_jti_and_an_expiry():
    """Both stores key on the jti; the table needs exp to bound itself."""
    from app.auth import decode_access_token

    token, jti = _token_and_jti()
    payload = decode_access_token(token)
    assert payload is not None
    assert jti
    assert payload["exp"] > time.time()
