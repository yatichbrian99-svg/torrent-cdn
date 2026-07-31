"""
Unit tests for upload routes: file upload with validation and MinIO integration.
"""
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest


class TestUpload:
    """Tests for POST /upload."""

    UPLOAD_URL = "/upload"

    def test_upload_success(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should upload a valid file and return file metadata."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        file_content = b"\x00\x00\x00\x20ftypmp42"
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test-video.mp4", BytesIO(file_content), "video/mp4")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "file_id" in data
        assert data["filename"] == "test-video.mp4"
        assert "magnet_link" in data
        assert "embed_url" in data

    def test_upload_no_file(self, client, auth_headers):
        """Should reject upload without a file (422 validation)."""
        resp = client.post(self.UPLOAD_URL, headers=auth_headers)
        assert resp.status_code == 422

    def test_upload_unsupported_type(self, client, auth_headers):
        """Should reject upload with unsupported file extension."""
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("malware.exe", BytesIO(b"fake"), "application/x-msdownload")},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported file type"

    def test_upload_no_auth(self, client):
        """Should reject upload without auth token (401)."""
        resp = client.post(
            self.UPLOAD_URL,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 401

    def test_upload_sanitized_filename(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should sanitize dangerous characters in filenames."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("../../../etc/passwd.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "/" not in data["filename"]
        assert ".." not in data["filename"]

    def test_upload_minio_failure(self, client, auth_headers, mock_conn, mock_s3_client):
        """Should return 500 if MinIO upload fails."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            None,  # no usage record
        ]
        mock_s3_client.create_multipart_upload.side_effect = Exception("MinIO connection refused")

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 500
        assert "Storage upload failed" in resp.json()["detail"]

    def test_upload_user_not_found(self, client, auth_headers, mock_conn):
        """Should reject upload if authenticated user doesn't exist in DB."""
        mock_conn.fetchrow.return_value = None

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "User not found"

    def test_upload_exceeds_usage_limit(self, client, auth_headers, mock_conn):
        """Should reject upload when user exceeds their plan's usage limit."""
        mock_conn.fetchrow.side_effect = [
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},
            {"total_gb_used": 100.0},
        ]

        file_content = b"x" * (1024 * 1024)  # 1MB (0.001 GB) -> 100.0 + 0.001 = 100.001 > 100
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(file_content), "video/mp4")},
        )
        assert resp.status_code == 402
        assert "Usage limit exceeded" in resp.json()["detail"]
