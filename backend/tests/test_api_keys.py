"""
Tests for real API-key auth (tcdn_ keys).

Covers the full lifecycle added for the dashboard API-key box:
- generate → plaintext returned once, only its SHA-256 hash stored
- authenticate via `Authorization: Bearer tcdn_…` (validated in
  get_current_user_id, no JWT involved)
- unknown/revoked keys → 401
- rotate-on-generate (one active key per user)
- GET returns metadata only (never the secret); DELETE revokes

Mocked via the shared conftest fixtures (DB pool/Redis/S3), matching the rest
of the suite — no real infra.
"""
from app.auth import API_KEY_PREFIX, hash_api_key


def _key_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestApiKeyAuthDependency:
    def test_valid_api_key_authenticates(self, client, mock_conn, test_user_id):
        """A tcdn_ key in the Bearer header resolves to the owner's user_id."""
        mock_conn.fetchrow.return_value = {"user_id": str(test_user_id)}
        r = client.get("/auth/me", headers=_key_headers(f"{API_KEY_PREFIX}validsecret123"))
        assert r.status_code == 200

    def test_unknown_api_key_rejected(self, client, mock_conn):
        """A key with no matching hash row is rejected with 401."""
        mock_conn.fetchrow.return_value = None
        r = client.get("/auth/me", headers=_key_headers(f"{API_KEY_PREFIX}unknownsecret123"))
        assert r.status_code == 401
        assert "Invalid or revoked API key" in r.json()["detail"]

    def test_revoked_api_key_rejected(self, client, mock_conn):
        """After revocation the hash row is gone — subsequent use is 401."""
        mock_conn.fetchrow.return_value = None
        r = client.get("/files", headers=_key_headers(f"{API_KEY_PREFIX}wasrevoked"))
        assert r.status_code == 401


class TestApiKeyEndpoints:
    def test_create_returns_plaintext_once_and_stores_only_hash(
        self, client, mock_conn, auth_headers
    ):
        mock_conn.fetchrow.return_value = {"created_at": "2026-08-04T00:00:00Z"}
        r = client.post("/auth/api-key", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"].startswith(API_KEY_PREFIX)
        assert body["prefix"].endswith("...")

        # The plaintext must never be stored — only its hash.
        insert_calls = [
            c for c in mock_conn.fetchrow.call_args_list if "INSERT INTO api_keys" in c.args[0]
        ]
        assert len(insert_calls) == 1
        stored_hash = insert_calls[0].args[2]
        assert stored_hash == hash_api_key(body["api_key"])
        assert stored_hash != body["api_key"]
        # The response itself must not contain the hash either.
        assert stored_hash not in r.text

    def test_generate_rotates_previous_key(self, client, mock_conn, auth_headers):
        """One active key per user: the rotate upsert replaces the old key's hash."""
        mock_conn.fetchrow.return_value = {"created_at": "2026-08-04T00:00:00Z"}
        key1 = client.post("/auth/api-key", headers=auth_headers).json()["api_key"]
        key2 = client.post("/auth/api-key", headers=auth_headers).json()["api_key"]
        assert key1 != key2

        # Rotation is a single atomic upsert ON CONFLICT (user_id) — the old
        # key is replaced, never a racy DELETE+INSERT pair.
        upserts = [
            c for c in mock_conn.fetchrow.call_args_list if "INSERT INTO api_keys" in c.args[0]
        ]
        assert len(upserts) == 2
        for u in upserts:
            assert "ON CONFLICT (user_id)" in u.args[0]
        # The two generates stored two different hashes (old key effectively gone).
        stored_hashes = {u.args[2] for u in upserts}
        assert len(stored_hashes) == 2
        assert hash_api_key(key1) in stored_hashes
        assert hash_api_key(key2) in stored_hashes
        # No DELETE is used by the rotate path at all.
        deletes = [
            c.args[0] for c in mock_conn.execute.call_args_list
            if "DELETE FROM api_keys" in c.args[0]
        ]
        assert deletes == []

    def test_get_returns_metadata_without_secret(self, client, mock_conn, auth_headers):
        mock_conn.fetchrow.return_value = {
            "prefix": "tcdn_abcd1234ef...",
            "created_at": "2026-08-01T00:00:00Z",
            "last_used_at": None,
        }
        r = client.get("/auth/api-key", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["api_key"] is None  # never recoverable
        assert body["prefix"] == "tcdn_abcd1234ef..."
        assert body["created_at"] is not None

    def test_get_returns_empty_when_no_key(self, client, mock_conn, auth_headers):
        mock_conn.fetchrow.return_value = None
        r = client.get("/auth/api-key", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"api_key": None, "prefix": None, "created_at": None, "last_used_at": None}

    def test_revoke_deletes_the_key(self, client, mock_conn, auth_headers):
        r = client.delete("/auth/api-key", headers=auth_headers)
        assert r.status_code == 200
        deletes = [
            c.args[0] for c in mock_conn.execute.call_args_list
            if "DELETE FROM api_keys" in c.args[0]
        ]
        assert len(deletes) == 1
        assert "WHERE user_id = $1" in deletes[0]

    def test_optional_auth_resolves_api_key(self, mock_conn, test_user_id):
        """get_optional_user_id (embed/analytics) accepts a valid tcdn_ key too."""
        import asyncio

        from app.auth import API_KEY_PREFIX, get_optional_user_id

        mock_conn.fetchrow.return_value = {"user_id": str(test_user_id)}

        class FakeCredentials:
            credentials = f"{API_KEY_PREFIX}validoptional123"

        result = asyncio.run(get_optional_user_id(FakeCredentials()))
        assert result == test_user_id

    def test_optional_auth_rejects_unknown_key(self, mock_conn):
        """An unknown tcdn_ key resolves to None in the optional dependency."""
        import asyncio

        from app.auth import API_KEY_PREFIX, get_optional_user_id

        mock_conn.fetchrow.return_value = None

        class FakeCredentials:
            credentials = f"{API_KEY_PREFIX}unknownoptional"

        assert asyncio.run(get_optional_user_id(FakeCredentials())) is None

    def test_endpoints_require_auth(self, client):
        for method in ("GET", "POST", "DELETE"):
            r = getattr(client, method.lower())("/auth/api-key")
            assert r.status_code == 401, f"{method} /auth/api-key without auth must be 401"
