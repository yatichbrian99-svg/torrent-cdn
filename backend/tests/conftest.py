"""
Test configuration and fixtures for TorrentCDN API tests.
Mocks all external dependencies (DB, Redis, MinIO, torrent) for unit testing.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from starlette.testclient import TestClient

# Set test environment before importing app modules
os.environ.setdefault("JWT_SECRET", "this-is-a-32-char-secret-for-testing-only!!")
os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="function")
def test_user_id() -> UUID:
    return UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture(scope="function")
def test_file_id() -> UUID:
    return UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture(scope="function")
def mock_conn():
    """Mock database connection — fresh per test."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value=None)
    return conn


@pytest.fixture(scope="function")
def mock_pool(mock_conn):
    """Mock database pool returning mock connection. Fresh per test."""
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=pool)
    pool.__aenter__ = AsyncMock(return_value=mock_conn)
    pool.__aexit__ = AsyncMock(return_value=None)
    return pool


@pytest.fixture(scope="function")
def mock_redis():
    """Mock Redis client — fresh per test."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.ping = AsyncMock(return_value=True)
    redis.ttl = AsyncMock(return_value=10)
    return redis


@pytest.fixture(scope="function")
def mock_s3_client():
    """Mock S3 (MinIO) client — fresh per test."""
    s3 = MagicMock()
    s3.head_bucket = MagicMock(return_value=True)
    s3.create_bucket = MagicMock(return_value=True)
    s3.create_multipart_upload = MagicMock(return_value={"UploadId": "test-upload-id"})
    s3.upload_part = MagicMock(return_value={"ETag": "test-etag"})
    s3.complete_multipart_upload = MagicMock(return_value=True)
    s3.delete_object = MagicMock(return_value=True)
    return s3


@pytest.fixture(autouse=True, scope="function")
def apply_mocks(mock_pool, mock_redis, mock_s3_client, monkeypatch):
    """
    Apply all mocks automatically for every test.
    Uses monkeypatch to ensure patches apply regardless of import style.
    """
    # Import route modules so they exist in sys.modules before we patch their local refs
    import app.routes.auth
    import app.routes.upload
    import app.routes.files
    import app.routes.billing
    import app.routes.analytics
    import app.routes.csp
    import app.routes.admin

    # Patch each route module's local reference imported via `from ..database import ...`
    monkeypatch.setattr(app.routes.auth, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.upload, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.files, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.billing, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.analytics, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.admin, "get_db_pool", AsyncMock(return_value=mock_pool))

    # Patch Redis in route modules that import it
    monkeypatch.setattr(app.routes.auth, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.billing, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.analytics, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.csp, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.upload, "get_redis", AsyncMock(return_value=mock_redis))

    # app/auth.py's get_current_user_id now reads the pw_change revocation
    # marker via get_redis — patch its local reference too (a real connection
    # attempt per authed request would slow the suite and pollute output).
    import app.auth
    monkeypatch.setattr(app.auth, "get_redis", AsyncMock(return_value=mock_redis))

    # app/routes/admin.py bumps pw_change after privilege changes
    import app.routes.admin
    monkeypatch.setattr(app.routes.admin, "get_redis", AsyncMock(return_value=mock_redis))

    # Patch security validation functions so tests don't need real magic bytes
    # Must patch both the module AND the importing module's local reference
    import app.security
    import app.routes.upload
    for mod in (app.security, app.routes.upload):
        monkeypatch.setattr(mod, "validate_file_magic", lambda fs, fn: (True, ""))
        monkeypatch.setattr(mod, "validate_content_type", lambda ct, fn: (True, ""))
        monkeypatch.setattr(mod, "estimate_compression_ratio", lambda fs: None)

    # Patch MinIO client (defined locally in upload.py; imported dynamically in files.py)
    monkeypatch.setattr(app.routes.upload, "get_minio_client", lambda: mock_s3_client)

    # Also patch the database module itself for any direct references
    import app.database
    monkeypatch.setattr(app.database, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.database, "get_redis", AsyncMock(return_value=mock_redis))


@pytest.fixture(scope="function")
def client():
    """FastAPI TestClient — fresh per test."""
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def admin_user_id() -> UUID:
    """A separate user ID that acts as the admin."""
    return UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.fixture(scope="function")
def auth_headers(client, test_user_id):
    """Generate valid JWT auth headers for a regular (non-admin) test user."""
    from app.auth import create_access_token
    token = create_access_token({"sub": str(test_user_id)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def admin_headers(client, admin_user_id):
    """Generate valid JWT auth headers for an admin user."""
    from app.auth import create_access_token
    token = create_access_token({"sub": str(admin_user_id)})
    return {"Authorization": f"Bearer {token}"}
