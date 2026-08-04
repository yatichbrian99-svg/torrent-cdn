"""
Unit tests for the chunked-upload fallback endpoints: /upload/session,
/upload/chunk/{upload_id}/{index} and /upload/finalize/{upload_id}.

These back the client-side chunking fallback: when a direct upload to the
backend fails transiently (documented Render free-tier POST flakiness), the
frontend re-ships the file in ≤4MB chunks through the same-origin Vercel proxy
(each chunk under the proxy's ~4.5MB request-body cap).
"""
import os
from io import BytesIO
from uuid import UUID

import app.routes.upload as upload_route


class TestChunkedUpload:
    SESSION_URL = "/upload/session"

    TEST_USER = {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"}
    VALID_MP4 = b"\x00\x00\x00\x20ftypmp42"  # 16 bytes, matches .mp4 magic

    def _cleanup_session(self, upload_id):
        """Remove the session + assembly temp file left behind by a test."""
        session = upload_route._chunk_sessions.pop(upload_id, None)
        if session:
            path = session.get("path")
            if path and os.path.exists(path):
                os.remove(path)

    def _create_session(self, client, auth_headers, mock_conn, filename="test-video.mp4",
                        file_size=26, total_chunks=2, idempotency_key=None):
        # Session create fetchrow order: dedup (only when a key is sent) -> user -> usage
        if idempotency_key:
            mock_conn.fetchrow.side_effect = [None, self.TEST_USER, None]
        else:
            mock_conn.fetchrow.side_effect = [self.TEST_USER, None]
        payload = {
            "filename": filename,
            "file_size": file_size,
            "total_chunks": total_chunks,
            "content_type": "video/mp4",
        }
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        resp = client.post(self.SESSION_URL, headers=auth_headers, json=payload)
        assert resp.status_code == 200
        return resp.json()["upload_id"]

    def test_session_creates(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.side_effect = [
            None,  # dedup
            self.TEST_USER,
            None,  # usage
        ]
        resp = client.post(
            self.SESSION_URL,
            headers=auth_headers,
            json={
                "filename": "test-video.mp4",
                "file_size": 5 * 1024 * 1024,
                "total_chunks": 2,
                "content_type": "video/mp4",
                "idempotency_key": "chunk-key-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["upload_id"]
        assert data["result"] is None
        self._cleanup_session(data["upload_id"])

    def test_session_rejects_unsupported_type(self, client, auth_headers):
        resp = client.post(
            self.SESSION_URL,
            headers=auth_headers,
            json={"filename": "malware.exe", "file_size": 1000, "total_chunks": 1},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported file type"

    def test_session_rejects_oversized_file(self, client, auth_headers):
        resp = client.post(
            self.SESSION_URL,
            headers=auth_headers,
            json={"filename": "big.mp4", "file_size": 2 * 1024 * 1024 * 1024, "total_chunks": 512},
        )
        assert resp.status_code == 413
        assert "File too large" in resp.json()["detail"]

    def test_session_rejects_empty_file(self, client, auth_headers):
        resp = client.post(
            self.SESSION_URL,
            headers=auth_headers,
            json={"filename": "empty.mp4", "file_size": 0, "total_chunks": 1},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "File is empty"

    def test_session_dedup_returns_existing(self, client, auth_headers, mock_conn):
        """A completed upload for the key returns as `result` — client skips chunks."""
        existing_id = UUID("33333333-3333-4333-8333-333333333333")
        mock_conn.fetchrow.return_value = {
            "id": existing_id,
            "filename": "test-video.mp4",
            "minio_url": "https://storage.example/bucket/user/test-video.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:testhash",
            "file_size_mb": 4.0,
            "thumbnail_url": None,
        }
        resp = client.post(
            self.SESSION_URL,
            headers=auth_headers,
            json={
                "filename": "test-video.mp4",
                "file_size": 4 * 1024 * 1024,
                "total_chunks": 1,
                "idempotency_key": "done-key",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["upload_id"] is None
        assert data["result"]["file_id"] == str(existing_id)

    def test_chunk_upload_and_finalize_end_to_end(self, client, auth_headers, mock_conn, mock_s3_client):
        # Session: dedup(None) -> user -> usage.  Finalize: dedup(None) -> user -> usage -> INSERT(id)
        mock_conn.fetchrow.side_effect = [
            None,
            self.TEST_USER,
            None,
            None,
            self.TEST_USER,
            None,
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]
        # Realistic 2-chunk layout: chunk 1 sits at the 4MB offset, so the
        # declared size must cover it (a 26-byte declared size with a 2-chunk
        # session was never realistic — chunk 0 writes 16 bytes at offset 0,
        # chunk 1 writes at offset 4MB).
        declared_size = 4 * 1024 * 1024 + len(self.VALID_MP4) + len(b"tail-bytes")
        upload_id = self._create_session(
            client, auth_headers, mock_conn, file_size=declared_size, total_chunks=2, idempotency_key="chunk-e2e"
        )
        # NOTE: _create_session resets side_effect — restore the full sequence.
        mock_conn.fetchrow.side_effect = [
            None,
            self.TEST_USER,
            None,
            None,
            self.TEST_USER,
            None,
            {"id": UUID("22222222-2222-4222-8222-222222222222")},
        ]

        resp0 = client.post(
            f"/upload/chunk/{upload_id}/0",
            headers=auth_headers,
            files={"chunk": ("chunk-0", BytesIO(self.VALID_MP4), "application/octet-stream")},
        )
        assert resp0.status_code == 200
        assert resp0.json() == {"received": 1, "total": 2}

        resp1 = client.post(
            f"/upload/chunk/{upload_id}/1",
            headers=auth_headers,
            files={"chunk": ("chunk-1", BytesIO(b"tail-bytes"), "application/octet-stream")},
        )
        assert resp1.status_code == 200
        assert resp1.json() == {"received": 2, "total": 2}

        final = client.post(f"/upload/finalize/{upload_id}", headers=auth_headers)
        assert final.status_code == 200
        data = final.json()
        assert "file_id" in data
        assert data["filename"] == "test-video.mp4"
        # The assembled file went through the full storage pipeline
        mock_s3_client.create_multipart_upload.assert_called()
        self._cleanup_session(upload_id)

    def test_finalize_rejects_incomplete(self, client, auth_headers, mock_conn):
        upload_id = self._create_session(client, auth_headers, mock_conn, file_size=26, total_chunks=2)
        client.post(
            f"/upload/chunk/{upload_id}/0",
            headers=auth_headers,
            files={"chunk": ("chunk-0", BytesIO(self.VALID_MP4), "application/octet-stream")},
        )
        final = client.post(f"/upload/finalize/{upload_id}", headers=auth_headers)
        assert final.status_code == 400
        assert "Upload incomplete" in final.json()["detail"]
        self._cleanup_session(upload_id)

    def test_chunk_rejects_oversized_chunk(self, client, auth_headers, mock_conn):
        upload_id = self._create_session(client, auth_headers, mock_conn, file_size=26, total_chunks=2)
        resp = client.post(
            f"/upload/chunk/{upload_id}/0",
            headers=auth_headers,
            files={"chunk": ("chunk-0", BytesIO(b"x" * (4 * 1024 * 1024 + 1)), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Chunk too large"
        self._cleanup_session(upload_id)

    def test_chunk_rejects_invalid_index(self, client, auth_headers, mock_conn):
        upload_id = self._create_session(client, auth_headers, mock_conn, file_size=26, total_chunks=2)
        resp = client.post(
            f"/upload/chunk/{upload_id}/5",
            headers=auth_headers,
            files={"chunk": ("chunk-5", BytesIO(b"abc"), "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid chunk index"
        self._cleanup_session(upload_id)

    def test_chunk_rejects_bad_session(self, client, auth_headers):
        resp = client.post(
            "/upload/chunk/does-not-exist/0",
            headers=auth_headers,
            files={"chunk": ("chunk-0", BytesIO(b"abc"), "application/octet-stream")},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Upload session not found"
