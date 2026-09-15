"""Tests for the live channel management API: CRUD, stream-key generation and
rotation, ownership scoping, and derived status."""
import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.live_models import hash_stream_key, new_slug, new_stream_key, stream_key_prefix
from app.mediamtx import mint_playback_token, verify_playback_token


class TestChannelCrud:
    URL = "/api/live/channels"

    def test_requires_auth(self, client):
        resp = client.get(self.URL)
        assert resp.status_code == 401

    def test_create_channel(self, client, auth_headers, mock_conn, test_user_id):
        mock_conn.fetch.return_value = []  # no existing slugs
        mock_conn.fetchrow.return_value = {
            "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "My Stream", "slug": "my-stream", "description": None,
            "stream_key_prefix": "live_abcd…", "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        resp = client.post(self.URL, json={"name": "My Stream"}, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["slug"] == "my-stream"
        assert data["status"] == "OFFLINE"
        # OBS setup card: the plaintext key + paste-ready URLs are returned
        # EXACTLY ONCE here (creation), then stored hashed — never listable.
        assert data["stream_key"].startswith("live_")
        assert data["publish_path"] == "live/my-stream"
        assert data["rtmp_server"].startswith("rtmp://")
        # The stream key is stored hashed, never in plaintext
        sql = mock_conn.fetchrow.await_args.args[0]
        assert "stream_key_hash" in sql
        assert "stream_key_hash, stream_key_prefix" not in data.values()

    def test_create_generates_unique_slugs(self, client, auth_headers, mock_conn, test_user_id):
        mock_conn.fetch.return_value = [{"slug": "my-stream"}]
        captured = {}
        def fake_fetchrow(sql, *args):
            captured["args"] = args
            return {
                "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
                "name": "My Stream", "slug": args[2], "description": None,
                "stream_key_prefix": "p", "status": "OFFLINE", "enabled": True,
                "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
                "last_seen_at": None,
            }
        mock_conn.fetchrow.side_effect = fake_fetchrow
        resp = client.post(self.URL, json={"name": "My Stream"}, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["slug"] == "my-stream-2"

    def test_list_scoped_to_owner(self, client, auth_headers, mock_conn):
        mock_conn.fetch.return_value = []
        resp = client.get(self.URL, headers=auth_headers)
        assert resp.status_code == 200
        sql = mock_conn.fetch.await_args.args[0]
        assert "owner_id = $1" in sql

    def test_get_other_tenant_channel_is_404(self, client, auth_headers, mock_conn):
        mock_conn.fetchrow.return_value = None
        resp = client.get(f"{self.URL}/{uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_clears_ephemeral_state(self, client, auth_headers, mock_conn, test_user_id):
        ch = {
            "id": uuid4(), "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "C", "slug": "gone", "description": None,
            "stream_key_prefix": "p", "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        mock_conn.fetchrow.return_value = ch
        with patch("app.routes.live.clear_channel", new=AsyncMock()) as cc:
            resp = client.delete(f"{self.URL}/{ch['id']}", headers=auth_headers)
        assert resp.status_code == 200
        cc.assert_awaited_once_with("gone")


class TestStreamKeys:
    def test_key_format_and_entropy(self):
        k1, k2 = new_stream_key(), new_stream_key()
        assert k1.startswith("live_")
        assert k1 != k2
        assert len(k1) > 40

    def test_hash_is_sha256_of_plaintext(self):
        key = new_stream_key()
        assert hash_stream_key(key) == hashlib.sha256(key.encode()).hexdigest()

    def test_prefix_never_contains_secret(self):
        key = new_stream_key()
        prefix = stream_key_prefix(key)
        assert key not in prefix
        assert len(prefix) <= 12

    def test_rotate_key_returns_plaintext_once(self, client, auth_headers, mock_conn, test_user_id):
        ch_id = uuid4()
        mock_conn.fetchrow.return_value = {
            "id": ch_id, "tenant_id": test_user_id, "owner_id": test_user_id,
            "name": "C", "slug": "c", "description": None, "stream_key_prefix": "p",
            "status": "OFFLINE", "enabled": True,
            "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC),
            "last_seen_at": None,
        }
        resp = client.post(f"/api/live/channels/{ch_id}/rotate-key", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stream_key"].startswith("live_")
        # Reveal/rotate also refreshes the paste-ready OBS URLs.
        assert data["publish_path"] == "live/c"
        assert data["rtmp_server"].startswith("rtmp://")
        # The UPDATE stores only the hash
        update_sql = mock_conn.execute.await_args.args[0]
        assert "stream_key_hash" in update_sql

    def test_slug_collision_suffix(self):
        existing = {"abc"}
        assert new_slug("ABC", existing) == "abc-2"
        existing.add("abc-2")
        assert new_slug("ABC", existing) == "abc-3"


class TestPlaybackTokens:
    def test_roundtrip(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=60)
        assert verify_playback_token(token, cid)

    def test_wrong_channel_rejected(self):
        from uuid import uuid4
        token = mint_playback_token(str(uuid4()), ttl_seconds=60)
        assert not verify_playback_token(token, str(uuid4()))

    def test_expired_rejected(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=-10)
        assert not verify_playback_token(token, cid)

    def test_tampered_rejected(self):
        from uuid import uuid4
        cid = str(uuid4())
        token = mint_playback_token(cid, ttl_seconds=60)
        payload, sig = token.split(".", 1)
        assert not verify_playback_token(f"{payload}.{'0' * len(sig)}", cid)

    def test_garbage_rejected(self):
        assert not verify_playback_token("not-a-token", "whatever")


class TestDerivedStatus:
    def _row(self, **over):
        base = {
            "id": uuid4(), "slug": "c", "enabled": True, "status": "OFFLINE",
            "last_seen_at": None,
        }
        base.update(over)
        return base

    async def _derive(self, row, *, mm_path=None, meta=None):
        with patch("app.routes.live.get_path_by_ingress", new=AsyncMock(return_value=mm_path)), \
             patch("app.routes.live.get_channel_meta", new=AsyncMock(return_value=meta or {})):
            from app.routes.live import derive_channel_status
            return await derive_channel_status(row)

    def test_disabled_is_offline(self):
        import asyncio
        assert asyncio.run(self._derive(self._row(enabled=False), mm_path={"ready": True})) == "OFFLINE"

    def test_mediamtx_ready_is_live(self):
        import asyncio
        assert asyncio.run(self._derive(self._row(), mm_path={"ready": True})) == "LIVE"

    def test_fresh_segment_is_live(self):
        import asyncio
        meta = {"last_seg_ts": str(__import__("time").time() - 1)}
        assert asyncio.run(self._derive(self._row(), meta=meta)) == "LIVE"

    def test_stale_segment_is_starting(self):
        import asyncio
        import time as t
        meta = {"last_seg_ts": str(t.time() - 20)}
        assert asyncio.run(self._derive(self._row(), meta=meta)) == "STARTING"

    def test_nothing_is_offline(self):
        import asyncio
        assert asyncio.run(self._derive(self._row())) == "OFFLINE"


class TestSchemaMatchesQueries:
    """Regression guard for the prod 500: live_channels DDL lacked
    stream_key_prefix while every query selected/inserted it (tests mock the
    DB, so the mismatch only surfaced in production). Parses the SQL in
    routes/live.py and asserts every referenced column exists in the DDL."""

    def _ddl_columns(self, table: str) -> set[str]:
        import re
        from app.database import LIVE_TABLES_DDL
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\);", LIVE_TABLES_DDL, re.S)
        assert m, f"{table} DDL not found"
        cols = set()
        for line in m.group(1).splitlines():
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("CREATE INDEX", "UNIQUE(", "CONSTRAINT")):
                continue
            name = line.split()[0]
            if name.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}:
                continue
            cols.add(name)
        return cols

    def _statements(self) -> list[str]:
        import re
        from app.routes import live as live_routes
        src = inspect.getsource(live_routes)
        return [m.group(1) for m in re.finditer(r'"""(SELECT|INSERT|UPDATE|DELETE)[^"]*"""', src, re.I)]

    def test_every_quoted_live_channels_column_exists_in_ddl(self):
        import re
        cols = self._ddl_columns("live_channels")
        for stmt in self._statements():
            if "live_channels" not in stmt:
                continue
            # SELECT lists: capture column tokens between SELECT and FROM
            for sel in re.finditer(r"SELECT(.*?)FROM", stmt, re.S | re.I):
                for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", sel.group(1)):
                    name = tok.group(0)
                    if name.upper() in {"SELECT", "DISTINCT", "AS", "COUNT", "NOW", "NOW()", "FROM"}:
                        continue
                    assert name in cols, f"live_channels query references unknown column {name!r} in: {stmt[:120]}"
            # INSERT column lists
            for ins in re.finditer(r"INSERT INTO live_channels \((.*?)\)", stmt, re.S | re.I):
                for tok in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", ins.group(1)):
                    assert tok.group(0) in cols, f"INSERT references unknown column {tok.group(0)!r}"
            # UPDATE SET lists
            for upd in re.finditer(r"UPDATE live_channels SET(.*?)(?:WHERE|$)", stmt, re.S | re.I):
                for tok in re.finditer(r"SET\s*,?\s*|([A-Za-z_][A-Za-z0-9_]*)\s*=", upd.group(1)):
                    if tok.group(1):
                        assert tok.group(1) in cols, f"UPDATE references unknown column {tok.group(1)!r}"

    def test_stream_key_prefix_in_ddl(self):
        assert "stream_key_prefix" in self._ddl_columns("live_channels")
