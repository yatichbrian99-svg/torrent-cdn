"""
DFS M1 tests: folder/rename on files (PATCH /files/{file_id}).

Harness conventions (conftest): DB pool/connection and Redis are AsyncMocks;
`client` is the TestClient; `auth_headers` carries a valid JWT for
test_user_id. Every DB statement in the route is `AND user_id = $...`, so the
mock exercises the same call-shape the production SQL uses.

Mocked-DB convention for these tests: `mock_conn.fetchrow` returns the CURRENT
row for ownership lookups, `mock_conn.fetchval` returns 0 (no conflict) unless
a test sets a clash.
"""
from datetime import UTC, datetime
from uuid import UUID

OTHER_USER = UUID("33333333-3333-4333-8333-333333333333")


def _row(file_id, user_id, filename="a.mp4", folder=None):
    return {
        "id": file_id,
        "user_id": user_id,
        "filename": filename,
        "folder_path": folder,
    }


class TestValidation:
    URL = "/files"

    def test_rejects_path_separator(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = _row(test_file_id, UUID(int=0x1111))
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"folder_path": "a/b"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_rejects_traversal_like_dot_segments(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = _row(test_file_id, UUID(int=0x1111))
        for bad in ("..", ".", ".hidden"):
            resp = client.patch(
                f"{self.URL}/{test_file_id}",
                json={"folder_path": bad},
                headers=auth_headers,
            )
            assert resp.status_code == 400, bad

    def test_rejects_control_characters(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = _row(test_file_id, UUID(int=0x1111))
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"folder_path": "bad\tname"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_empty_string_means_root(self, client, auth_headers, test_file_id, mock_conn):
        """folder_path=\"\" normalizes to root (None) and still writes."""
        mock_conn.fetchrow.return_value = _row(test_file_id, UUID(int=0x1111), folder="Videos")
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"folder_path": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["folder_path"] is None


class TestRenameAndMove:
    URL = "/files"

    def test_rename_updates_row(self, client, auth_headers, test_file_id, mock_conn):
        uid = UUID(int=0x1111)
        mock_conn.fetchrow.return_value = _row(test_file_id, uid, filename="old.mp4")
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"filename": "new.mp4"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["filename"] == "new.mp4"
        # Ensure UPDATE ran with the new name
        update_sql = mock_conn.execute.call_args[0][0]
        assert "SET filename = $1, folder_path = $2" in update_sql

    def test_move_to_folder(self, client, auth_headers, test_file_id, mock_conn):
        uid = UUID(int=0x1111)
        mock_conn.fetchrow.return_value = _row(test_file_id, uid, folder=None)
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"folder_path": "Videos"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["folder_path"] == "Videos"

    def test_nochange_is_idempotent_200(self, client, auth_headers, test_file_id, mock_conn):
        """Renaming to the SAME name (no-op) skips the conflict path and 200s."""
        uid = UUID(int=0x1111)
        mock_conn.fetchrow.return_value = _row(test_file_id, uid, filename="same.mp4")
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"filename": "same.mp4"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

    def test_target_conflict_returns_409(self, client, auth_headers, test_file_id, mock_conn):
        uid = UUID(int=0x1111)
        mock_conn.fetchrow.return_value = _row(test_file_id, uid, filename="old.mp4")
        mock_conn.fetchval.return_value = 1  # a clash exists at the target
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"filename": "taken.mp4"},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    def test_missing_file_returns_404(self, client, auth_headers, test_file_id, mock_conn):
        mock_conn.fetchrow.return_value = None  # no row for THIS user
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"filename": "x.mp4"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestAuthorization:
    URL = "/files"

    def test_requires_auth(self, client, test_file_id):
        resp = client.patch(
            f"{self.URL}/{test_file_id}",
            json={"filename": "x.mp4"},
        )
        assert resp.status_code == 401


class TestSchemaAdditivity:
    URL = "/files"

    def test_folder_path_in_list_response(self, client, auth_headers, test_file_id, mock_conn):
        """GET /files stays additive: existing fields intact, folder_path present."""
        file_id = test_file_id
        uploads_rows = [
            {
                "id": file_id,
                "filename": "v.mp4",
                "minio_url": "https://x/y",
                "torrent_magnet": "magnet:?xt=urn:btih:x",
                "file_size_mb": 1.0,
                "thumbnail_url": None,
                "folder_path": "Movies",
                "created_at": datetime.now(UTC),
            }
        ]
        # The route calls conn.fetch twice: uploads rows, then analytics rows.
        mock_conn.fetch.side_effect = [uploads_rows, []]
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        item = resp.json()[0]
        # Old fields still present
        for legacy_key in ("id", "filename", "minio_url", "file_size_mb", "created_at"):
            assert legacy_key in item
        # New field populated
        assert item["folder_path"] == "Movies"
