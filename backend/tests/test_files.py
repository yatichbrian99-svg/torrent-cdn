"""
Unit tests for file routes: list, get, get public, and delete.
"""
import json
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.routes import files as files_route

FILE_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_FILE_ID = UUID("33333333-3333-4333-8333-333333333333")


def _make_file_row(fid=None):
    fid = fid or FILE_ID
    return {
        "id": fid,
        "filename": "test-video.mp4",
        "minio_url": f"http://minio:9000/bucket/key/{fid}.mp4",
        "torrent_magnet": "magnet:?xt=urn:btih:test",
        "file_size_mb": 10.5,
        "created_at": "2026-01-01T00:00:00Z",
    }


class TestListFiles:
    """Tests for GET /files."""

    LIST_URL = "/files"

    def test_list_files_empty(self, client, auth_headers, mock_conn):
        """Should return empty list when user has no files."""
        mock_conn.fetch.return_value = []

        resp = client.get(self.LIST_URL, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_files_with_results(self, client, auth_headers, mock_conn):
        """Should return list of user's files with analytics."""
        # First fetch returns files, second fetch returns analytics
        mock_conn.fetch.side_effect = [
            [_make_file_row()],  # file list
            [{"file_id": FILE_ID, "views": 5, "total_p2p": 800000, "total_origin": 200000}],  # analytics
        ]

        resp = client.get(self.LIST_URL, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["filename"] == "test-video.mp4"
        assert data[0]["file_size_mb"] == 10.5
        assert data[0]["views"] == 5
        assert data[0]["p2p_offload_percent"] == 80.0

    def test_list_files_no_auth(self, client):
        """Should reject without auth token (401)."""
        resp = client.get(self.LIST_URL)
        assert resp.status_code == 401


class TestGetFile:
    """Tests for GET /files/{file_id}."""

    def test_get_file_success(self, client, auth_headers, mock_conn):
        """Should return file details for owned file."""
        # First fetchrow = file lookup, second = analytics
        mock_conn.fetchrow.side_effect = [
            _make_file_row(),
            {"total_p2p": 800000, "total_origin": 200000, "views": 5},
        ]

        resp = client.get(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "test-video.mp4"
        assert data["id"] == str(FILE_ID)
        assert data["views"] == 5
        assert data["p2p_offload_percent"] == 80.0

    def test_get_file_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 for non-existent file."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(f"/files/{OTHER_FILE_ID}", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"


class TestGetPublicFile:
    """Tests for GET /files/public/{file_id}."""

    PUBLIC_URL = "/files/public"

    def test_get_public_file_success(self, client, mock_conn):
        """Should return file info without auth (public endpoint)."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "test-video.mp4"
        assert "magnet_link" in data

    def test_get_public_file_not_found(self, client, mock_conn):
        """Should return 404 for non-existent public file."""
        mock_conn.fetchrow.return_value = None

        resp = client.get(f"{self.PUBLIC_URL}/{OTHER_FILE_ID}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"

    def test_get_public_file_no_auth_required(self, client, mock_conn):
        """Should work without any auth headers (public)."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "public-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 5.0,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        assert resp.json()["filename"] == "public-video.mp4"

    def test_get_public_file_served_from_redis_cache(self, client, mock_conn, mock_redis):
        """A cached payload is answered from Redis without touching the DB."""
        payload = {
            "id": str(FILE_ID),
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "magnet_link": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        def get_side_effect(key):
            # Only the public-file cache key is warm; rate-limit keys stay cold.
            return json.dumps(payload) if key == f"public_file:{FILE_ID}" else None

        mock_redis.get.side_effect = get_side_effect
        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        assert resp.json() == payload
        mock_conn.fetchrow.assert_not_called()

    def test_get_public_file_populates_redis_cache_on_miss(self, client, mock_conn, mock_redis):
        """A cache miss serves from the DB and writes the payload for next time."""
        mock_conn.fetchrow.return_value = {
            "id": FILE_ID,
            "filename": "test-video.mp4",
            "minio_url": "http://minio:9000/bucket/key/test.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:test",
            "file_size_mb": 10.5,
        }

        resp = client.get(f"{self.PUBLIC_URL}/{FILE_ID}")
        assert resp.status_code == 200
        cache_sets = [
            c for c in mock_redis.setex.call_args_list
            if str(c.args[0]) == f"public_file:{FILE_ID}"
        ]
        assert len(cache_sets) == 1
        assert cache_sets[0].args[1] == files_route.PUBLIC_FILE_CACHE_TTL
        assert json.loads(cache_sets[0].args[2])["filename"] == "test-video.mp4"


class TestDeleteFile:
    """Tests for DELETE /files/{file_id}."""

    def test_delete_file_success(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should delete a file and return success."""
        mock_conn.fetchrow.return_value = {
            "minio_url": f"http://minio:9000/torrent-cdn-files/user123/file123/test.mp4",
        }

        resp = client.delete(f"/files/{FILE_ID}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["detail"] == "File deleted successfully"

    def test_delete_file_not_found(self, client, auth_headers, mock_conn):
        """Should return 404 when deleting non-existent file."""
        mock_conn.fetchrow.return_value = None

        resp = client.delete(f"/files/{OTHER_FILE_ID}", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "File not found"

    def test_delete_file_no_auth(self, client):
        """Should reject deletion without auth token (401)."""
        resp = client.delete(f"/files/{FILE_ID}")
        assert resp.status_code == 401
