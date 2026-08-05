"""
Unit tests for upload routes: file upload with validation and MinIO integration.
"""
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest


class TestTorrentAndThumbnail:
    """Unit tests for the shared torrent/thumbnail generator."""

    def test_no_existing_path_cleans_up_its_own_tempdir(self, tmp_path, mock_s3_client):
        """The generator writes its own temp file and removes its tempdir in
        the finally (no leaks on the direct/chunked path)."""
        from uuid import uuid4
        from app.routes.upload import _torrent_and_thumbnail

        stream = BytesIO(b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 4096)
        user_id = uuid4()
        file_id = str(uuid4())

        magnet, _ = _torrent_and_thumbnail(stream, "clip.webm", user_id, file_id, mock_s3_client)

        assert magnet.startswith("magnet:?xt=urn:btih:")
        assert "btih:" + file_id not in magnet
        # No torrentcd-* tempdirs left behind under the system temp root.
        import glob, os, tempfile
        leftovers = glob.glob(os.path.join(tempfile.gettempdir(), "torrentcd-*"))
        assert leftovers == [], f"leaked tempdirs: {leftovers}"


class TestStreamingTorrentBuilder:
    """The streaming infohash builder MUST stay byte-identical to torrentool's
    Torrent.create_from — a divergence would fork the swarm (browser seeders
    rebuild the info dict from the magnet, so a wrong infohash means peers can
    never find each other). These tests pin that identity."""

    def test_piece_length_rule(self):
        from app.torrent_builder import torrentool_piece_length

        assert torrentool_piece_length(1) == 32768
        assert torrentool_piece_length(32768) == 32768
        assert torrentool_piece_length(32769) == 262144
        assert torrentool_piece_length(262144) == 262144
        assert torrentool_piece_length(10 * 1024 * 1024) == 262144

    def test_streaming_infohash_byte_identical_to_torrentool(self, tmp_path):
        """Same bytes hashed via our streaming builder and via torrentool's
        create_from must produce the IDENTICAL bencoded info dict and infohash,
        for every piece-length boundary and reader chunk size."""
        import hashlib
        import os
        from torrentool.api import Torrent
        from torrentool.bencode import Bencode

        from app.torrent_builder import (
            bencode_info,
            hash_pieces_from_stream,
            torrentool_piece_length,
        )

        # 2621450 ≈ 10 pieces of 262144 — multi-piece coverage beyond the
        # 4-piece (1 MiB) boundary, plus every boundary + trailing partial.
        sizes = [1, 32767, 32768, 32769, 262143, 262144, 262145, 1048576, 2621450]
        chunk_sizes = [3, 4096, 16384]

        for size in sizes:
            path = tmp_path / f"clip-{size}.mp4"
            path.write_bytes(os.urandom(size))
            name = path.name  # torrentool names the torrent after the file basename

            theirs = Torrent.create_from(str(path))
            theirs_info = Bencode.encode(theirs._struct["info"])

            for cs in chunk_sizes:
                with open(path, "rb") as fh:
                    pieces = hash_pieces_from_stream(lambda: fh.read(cs), size)
                mine_info = bencode_info(name, pieces, torrentool_piece_length(size), size)
                assert mine_info == theirs_info, f"info bytes diverge at size={size} chunk={cs}"
                mine_hash = hashlib.sha1(mine_info).hexdigest()
                assert mine_hash == str(theirs.info_hash), f"infohash diverges at size={size} chunk={cs}"

    def test_build_magnet_format_and_infohash(self, tmp_path):
        """The magnet carries the real 40-hex infohash, the real dn, and all
        three canonical trackers as percent-encoded tr= params."""
        import os
        import urllib.parse

        from app.torrent_builder import (
            PUBLIC_TRACKERS,
            build_magnet,
            hash_pieces_from_stream,
        )

        size = 500_000
        path = tmp_path / "movie.webm"
        path.write_bytes(os.urandom(size))
        with open(path, "rb") as fh:
            pieces = hash_pieces_from_stream(lambda: fh.read(16384), size)

        magnet = build_magnet("movie.webm", pieces, size)

        assert magnet.startswith("magnet:?xt=urn:btih:")
        btih = magnet[len("magnet:?xt=urn:btih:"): magnet.index("&dn=")]
        assert len(btih) == 40
        assert all(c in "0123456789abcdef" for c in btih)
        assert "dn=movie.webm" in magnet
        for tr in PUBLIC_TRACKERS:
            assert f"tr={urllib.parse.quote(tr, safe='')}" in magnet
        assert magnet.count("&tr=") == len(PUBLIC_TRACKERS)


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

    def test_upload_idempotent_retry_returns_existing(self, client, auth_headers, mock_conn, mock_s3_client):
        """A retry with the same idempotency key returns the original upload — no duplicate."""
        existing_id = UUID("33333333-3333-4333-8333-333333333333")
        mock_conn.fetchrow.return_value = {
            "id": existing_id,
            "filename": "test-video.mp4",
            "minio_url": "https://storage.example/bucket/user/test-video.mp4",
            "torrent_magnet": "magnet:?xt=urn:btih:testhash",
            "file_size_mb": 0.001,
            "thumbnail_url": None,
        }

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test-video.mp4", BytesIO(b"\x00\x00\x00\x20ftypmp42"), "video/mp4")},
            data={"idempotency_key": "retry-key-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_id"] == str(existing_id)
        assert data["filename"] == "test-video.mp4"
        assert data["magnet_link"] == "magnet:?xt=urn:btih:testhash"
        assert data["embed_url"].endswith(f"/embed/{existing_id}")
        # The retry must not re-run storage, re-insert, or re-account usage —
        # no new file, no duplicate, no double-counted bandwidth
        mock_s3_client.create_multipart_upload.assert_not_called()
        mock_conn.execute.assert_not_called()

    def test_upload_with_idempotency_key_records_key(self, client, auth_headers, mock_conn, mock_s3_client):
        """A fresh idempotency key uploads normally and stores the key on the record."""
        mock_conn.fetchrow.side_effect = [
            None,  # dedup check — nothing stored for this key yet
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},  # user
            None,  # usage record
            {"id": UUID("22222222-2222-4222-8222-222222222222")},  # INSERT ... RETURNING id
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test-video.mp4", BytesIO(b"\x00\x00\x00\x20ftypmp42"), "video/mp4")},
            data={"idempotency_key": "fresh-key-1"},
        )
        assert resp.status_code == 200
        # The INSERT must carry the idempotency key as its last parameter
        insert_args = mock_conn.fetchrow.call_args_list[3].args
        assert insert_args[-1] == "fresh-key-1"

    def test_upload_idempotency_race_returns_winner(self, client, auth_headers, mock_conn, mock_s3_client):
        """If a concurrent same-key upload wins the insert, return its result — no duplicate."""
        from asyncpg.exceptions import UniqueViolationError

        winner_id = UUID("33333333-3333-4333-8333-333333333333")
        mock_conn.fetchrow.side_effect = [
            None,  # dedup check
            {"id": UUID("11111111-1111-4111-8111-111111111111"), "plan": "free"},  # user
            None,  # usage
            UniqueViolationError("duplicate key value violates unique constraint"),  # insert loses the race
            {  # re-fetch: the concurrent winner's completed record
                "id": winner_id,
                "filename": "test-video.mp4",
                "minio_url": "https://storage.example/bucket/user/file.mp4",
                "torrent_magnet": "magnet:?xt=urn:btih:winnerhash",
                "file_size_mb": 0.001,
                "thumbnail_url": None,
            },
        ]

        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test-video.mp4", BytesIO(b"\x00\x00\x00\x20ftypmp42"), "video/mp4")},
            data={"idempotency_key": "race-key-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_id"] == str(winner_id)
        assert data["magnet_link"] == "magnet:?xt=urn:btih:winnerhash"

    def test_upload_rejects_oversized_idempotency_key(self, client, auth_headers):
        """An idempotency key longer than 64 chars is rejected (400)."""
        resp = client.post(
            self.UPLOAD_URL,
            headers=auth_headers,
            files={"file": ("test.mp4", BytesIO(b"test"), "video/mp4")},
            data={"idempotency_key": "k" * 65},
        )
        assert resp.status_code == 400
        assert "idempotency_key too long" in resp.json()["detail"]

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
