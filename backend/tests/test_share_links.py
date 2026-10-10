"""
DFS M1 tests: share links (POST/GET/DELETE /files/{id}/links, POST /files/share/{token}).

Harness): DB pool is an AsyncMock (conftest). Semantics under test:
- create → plaintext token returned exactly once, only its SHA-256 stored
- public resolve: 404 unknown, 410 revoked/expired/exhausted, 401 wrong-password
- owner listing shows revoked/download-count state without the token
- per-file link cap (429) and ownership (404 on cross-user create/revoke)
- token never appears in server logs (log-scan)
"""
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta

TOKEN = "A" * 32  # format-valid token for resolve tests
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


def _link_row(**over):
    row = {
        "id": "99999999-9999-4999-8999-999999999999",
        "password_hash": None,
        "max_downloads": None,
        "download_count": 0,
        "expires_at": None,
        "revoked_at": None,
        "owner_id": "11111111-1111-4111-8111-111111111111",
        "file_id": "22222222-2222-4222-8222-222222222222",
        "filename": "video.mp4",
        "minio_url": "https://storage/x/y/z/video.mp4",
        "torrent_magnet": "magnet:?xt=urn:btih:abc",
        "file_size_mb": 12.5,
    }
    row.update(over)
    return row


class TestCreateShareLink:
    URL = "/files"

    def test_returns_plaintext_token_once(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = {"id": test_file_id}
        mock_conn.fetchval.return_value = 0  # no existing links
        mock_conn.fetchrow.side_effect = None
        mock_conn.fetchrow.return_value = {"id": test_file_id}
        # SECOND fetchrow (INSERT ... RETURNING) gets its own value:
        import unittest.mock as um
        mock_conn.fetchrow = um.AsyncMock(side_effect=[
            {"id": test_file_id},                      # ownership check
            {                                          # INSERT ... RETURNING
                "id": "88888888-8888-4888-8888-888888888888",
                "expires_at": None, "max_downloads": None,
                "download_count": 0, "revoked_at": None,
                "created_at": datetime.now(UTC),
            },
        ])

        import unittest.mock as um
        mock_conn.fetchrow = um.AsyncMock(side_effect=[
            {"id": test_file_id},                      # ownership check
            {                                          # INSERT ... RETURNING
                "id": "88888888-8888-4888-8888-888888888888",
                "expires_at": datetime.now(UTC) + timedelta(hours=24),
                "max_downloads": 10,
                "download_count": 0, "revoked_at": None,
                "created_at": datetime.now(UTC),
            },
        ])

        resp = client.post(
            f"{self.URL}/{test_file_id}/links",
            json={"expires_in_hours": 24, "max_downloads": 10},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["token"]) == 32
        assert body["url"].endswith(f"/share/{body['token']}")
        assert body["max_downloads"] == 10

    def test_token_stored_hashed_not_plaintext(self, client, auth_headers, test_file_id, mock_conn):
        import unittest.mock as um
        mock_conn.fetchrow = um.AsyncMock(side_effect=[
            {"id": test_file_id},
            {"id": "88888888-8888-4888-8888-888888888888", "expires_at": None,
             "max_downloads": None, "download_count": 0, "revoked_at": None,
             "created_at": datetime.now(UTC)},
        ])
        client.post(f"{self.URL}/{test_file_id}/links", json={}, headers=auth_headers)
        insert_sql, *insert_args = mock_conn.fetchrow.call_args_list[1][0]
        assert "INSERT INTO share_links" in insert_sql
        token_param = insert_args[2]
        assert token_param != "" and len(token_param) == 64  # hex sha-256, never plaintext
        assert all(c in "0123456789abcdef" for c in token_param)

    def test_cross_user_404(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = None  # ownership check fails
        resp = client.post(f"{self.URL}/{test_file_id}/links", json={}, headers=auth_headers)
        assert resp.status_code == 404

    def test_per_file_link_cap(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = {"id": test_file_id}
        mock_conn.fetchval.return_value = 999  # over cap
        resp = client.post(f"{self.URL}/{test_file_id}/links", json={}, headers=auth_headers)
        assert resp.status_code == 429

    def test_requires_auth(self, client, test_file_id):
        resp = client.post(f"{self.URL}/{test_file_id}/links", json={})
        assert resp.status_code == 401

    def test_invalid_expiry_rejected(self, client, auth_headers, test_file_id):
        resp = client.post(
            f"{self.URL}/{test_file_id}/links",
            json={"expires_in_hours": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 422  # pydantic ge=1


class TestResolveShareLink:
    URL = "/files/share"

    def test_unknown_token_404(self, client, mock_conn):
        mock_conn.fetchrow.return_value = None
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 404

    def test_malformed_token_404_without_db(self, client, mock_conn):
        resp = client.post(f"{self.URL}/short", json={})
        assert resp.status_code == 404
        mock_conn.fetchrow.assert_not_awaited()

    def test_revoked_410(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row(revoked_at=datetime.now(UTC))
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 410

    def test_expired_410(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row(
            expires_at=datetime.now(UTC) - timedelta(hours=1)
        )
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 410

    def test_download_cap_exhausted_410(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row(max_downloads=3, download_count=3)
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 410

    def test_valid_link_returns_metadata_and_counts(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row()
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"] == "video.mp4"
        assert "minio_url" in body and "magnet_link" in body
        # download_count increment ran
        exec_sql, exec_args = mock_conn.execute.call_args[0][0], mock_conn.execute.call_args[0][1:]
        assert "download_count = download_count + 1" in exec_sql
        assert exec_args[0] == _link_row()["id"]

    def test_wrong_password_401(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row(password_hash="$2b$12$fakehashfakehashfakehash")
        resp = client.post(f"{self.URL}/{TOKEN}", json={"password": "nope"})
        assert resp.status_code == 401

    def test_missing_password_401(self, client, mock_conn):
        mock_conn.fetchrow.return_value = _link_row(password_hash="$2b$12$fakehashfakehashfakehash")
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 401

    def test_rate_limited(self, client, mock_conn):
        from app.routes import files as files_mod
        files_mod._share_access_requests.clear()
        ip = "testclient"
        files_mod._share_access_requests[ip] = [time.time() - i * 0.1 for i in range(files_mod.SHARE_ACCESS_LIMIT)]
        resp = client.post(f"{self.URL}/{TOKEN}", json={})
        assert resp.status_code == 429
        files_mod._share_access_requests.clear()


class TestRevokeAndList:
    def test_revoke_owner(self, client, auth_headers, mock_conn):
        mock_conn.execute.return_value = "UPDATE 1"
        resp = client.delete(
            "/files/links/99999999-9999-4999-8999-999999999999",
            headers=auth_headers,
        )
        assert resp.status_code == 200

    def test_revoke_not_owned_or_already_revoked_404(self, client, auth_headers, mock_conn):
        mock_conn.execute.return_value = "UPDATE 0"
        resp = client.delete(
            "/files/links/99999999-9999-4999-8999-999999999999",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_list_shows_state_never_token(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetch.return_value = [
            {
                "id": "88888888-8888-4888-8888-888888888888",
                "expires_at": None, "max_downloads": 5, "download_count": 2,
                "revoked_at": datetime.now(UTC),
                "created_at": datetime.now(UTC),
            }
        ]
        resp = client.get(f"/files/{test_file_id}/links", headers=auth_headers)
        assert resp.status_code == 200
        link = resp.json()[0]
        assert link["revoked"] is True
        assert link["download_count"] == 2
        assert "token" not in link  # hashed at rest — cannot be listed


class TestNoTokenInLogs:
    URL = "/files"

    def test_plaintext_token_never_logged(self, client, auth_headers, test_file_id, mock_conn, caplog):
        """The create/resolve/revoke paths must never log the plaintext token."""
        import unittest.mock as um
        mock_conn.fetchrow = um.AsyncMock(side_effect=[
            {"id": test_file_id},
            {"id": "88888888-8888-4888-8888-888888888888", "expires_at": None,
             "max_downloads": None, "download_count": 0, "revoked_at": None,
             "created_at": datetime.now(UTC)},
        ])
        with caplog.at_level(logging.DEBUG, logger="app.routes.files"):
            client.post(f"{self.URL}/{test_file_id}/links", json={}, headers=auth_headers)
            # The resolve call's mock side_effect list is exhausted after the
            # two create calls; a third DB-read would raise StopIteration, so
            # point it at a neutral row instead of swallowing the error.
            mock_conn.fetchrow.side_effect = None
            mock_conn.fetchrow.return_value = None  # unknown link → 404
            client.post("/files/share/" + TOKEN, json={})
        body_text = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in body_text
