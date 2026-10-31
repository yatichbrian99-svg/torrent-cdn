"""
Test configuration and fixtures for TorrentCDN API tests.
Mocks all external dependencies (DB, Redis, MinIO, torrent) for unit testing.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import pytest_asyncio
from starlette.testclient import TestClient

# Set test environment before importing app modules
os.environ.setdefault("JWT_SECRET", "this-is-a-32-char-secret-for-testing-only!!")
os.environ.setdefault("APP_ENV", "development")
# Hermetic tests: a developer shell that exports MEDIAMTX_HOOK_SECRET (for
# docker compose) must not flip internal-hook endpoints into 403-ing the
# suite. The secret-enforcement test sets/overrides it explicitly.
os.environ.pop("MEDIAMTX_HOOK_SECRET", None)

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

    # Model asyncpg's transaction(): a SYNCHRONOUS call returning an async
    # context manager (calling an AsyncMock here would return a coroutine and
    # break `async with conn.transaction():`). Inside the tx block the routes
    # keep using the outer `conn` (execute/fetchrow), so __aenter__ just
    # returns it.
    class _FakeTx:
        def __init__(self, c):
            self._c = c

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            return False

    conn.transaction = MagicMock(return_value=_FakeTx(conn))
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


@pytest.fixture(scope="function")
def startup_pool():
    """Pool handed to the app's lifespan startup.

    Deliberately separate from ``mock_pool``/``mock_conn``: startup DDL must not
    appear in a test's own ``mock_conn.execute`` assertions (billing webhook
    tests assert *no* write happened). It exists so no test ever opens a real
    connection to DATABASE_URL — that connection attempt was the root cause of
    the intermittent full-suite stall (see docs/tcdn-ai/STATE.md).
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=0)

    class _FakeTx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *exc):
            return False

    conn.transaction = MagicMock(return_value=_FakeTx())
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=pool)
    pool.__aenter__ = AsyncMock(return_value=conn)
    pool.__aexit__ = AsyncMock(return_value=None)
    return pool


@pytest.fixture(autouse=True, scope="function")
def apply_mocks(mock_pool, mock_redis, mock_s3_client, startup_pool, monkeypatch):
    """
    Apply all mocks automatically for every test.
    Uses monkeypatch to ensure patches apply regardless of import style.
    """
    # Import route modules so they exist in sys.modules before we patch their local refs
    import app.routes.admin
    import app.routes.analytics
    import app.routes.auth
    import app.routes.billing
    import app.routes.csp
    import app.routes.files
    import app.routes.live
    import app.routes.live_internal
    import app.routes.payouts
    import app.routes.upload

    # Patch each route module's local reference imported via `from ..database import ...`
    monkeypatch.setattr(app.routes.auth, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.upload, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.files, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.billing, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.analytics, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.admin, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.payouts, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.live, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.live_internal, "get_db_pool", AsyncMock(return_value=mock_pool))
    # verification + notifications were NOT patched here — a module imported once
    # (app.main import) keeps the FIRST test's patched binding forever, so any
    # test exercising /auth/resend-verification or /notifications would read
    # the stale first-test mock_conn. Patch them like the rest.
    import app.routes.notifications
    import app.routes.verification
    monkeypatch.setattr(app.routes.verification, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.routes.notifications, "get_db_pool", AsyncMock(return_value=mock_pool))

    # Patch Redis in route modules that import it
    monkeypatch.setattr(app.routes.auth, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.billing, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.analytics, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.csp, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.upload, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.files, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.routes.live, "get_redis", AsyncMock(return_value=mock_redis))

    # app/auth.py's get_current_user_id now reads the pw_change revocation
    # marker via get_redis AND resolves tcdn_ API keys via get_db_pool — patch
    # both local references (a real connection attempt per authed request would
    # slow the suite and pollute output).
    import app.auth
    monkeypatch.setattr(app.auth, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(app.auth, "get_db_pool", AsyncMock(return_value=mock_pool))
    # The DB security_stamp revocation check (authoritative when Redis is
    # absent) is a separate helper so tests can stub it directly; default to
    # "no change" so the vast majority of tests behave as before. Tests that
    # exercise rotation override it (see TestSessionRevocationWithoutRedis).
    monkeypatch.setattr(app.auth, "get_user_security_stamp", AsyncMock(return_value=None))


    # Patch security validation functions so tests don't need real magic bytes
    # Must patch both the module AND the importing module's local reference
    import app.routes.upload
    import app.security
    for mod in (app.security, app.routes.upload):
        monkeypatch.setattr(mod, "validate_file_magic", lambda fs, fn: (True, ""))
        monkeypatch.setattr(mod, "validate_content_type", lambda ct, fn: (True, ""))
        monkeypatch.setattr(mod, "estimate_compression_ratio", lambda fs: None)

    # Patch MinIO client (defined locally in upload.py; imported dynamically in files.py)
    monkeypatch.setattr(app.routes.upload, "get_minio_client", lambda: mock_s3_client)
    # Reset the memoized bucket-ensure flag so each test sees a fresh "not yet
    # ensured" state (otherwise the first test's ensure suppresses the
    # head_bucket/put_bucket_cors calls for the rest of the suite).
    monkeypatch.setattr(app.routes.upload, "_bucket_ensured", False)

    # Also patch the database module itself for any direct references
    import app.database
    monkeypatch.setattr(app.database, "get_db_pool", AsyncMock(return_value=mock_pool))
    monkeypatch.setattr(app.database, "get_redis", AsyncMock(return_value=mock_redis))

    # The lifespan's startup migrations call main.py's MODULE-LEVEL `get_db_pool`
    # binding (`from .database import get_db_pool`), which none of the patches
    # above reach. Left unpatched, every TestClient startup opened a real asyncpg
    # pool against DATABASE_URL; when that host silently drops SYNs (a stopped
    # docker bridge IP) the fixture blocked for 30-120s at a time — the
    # intermittent "full suite hangs" bug. Patch it to a throwaway pool.
    import app.main
    monkeypatch.setattr(app.main, "get_db_pool", AsyncMock(return_value=startup_pool))

    # Live modules: patch their database references too (live_state / mediamtx
    # import get_redis at call time via .database, but routes/live.py and
    # live_internal.py hold local bindings — patched above). The live monitor
    # must NEVER run inside tests (it would touch real MediaMTX over HTTP).
    from app import live_state
    monkeypatch.setattr(live_state, "get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(live_state, "start_live_monitor", lambda: None)


@pytest.fixture(scope="function")
def client():
    """FastAPI TestClient — fresh per test."""
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def live_channel_row(test_user_id):
    """A live_channels DB row as asyncpg would return it (dict-like)."""
    from datetime import UTC, datetime
    from uuid import uuid4
    row = {
        "id": uuid4(),
        "tenant_id": test_user_id,
        "owner_id": test_user_id,
        "name": "Test Channel",
        "slug": "test-channel",
        "description": "A test channel",
        "stream_key_hash": "a" * 64,
        "stream_key_prefix": "live_abcd…",
        "status": "OFFLINE",
        "enabled": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "last_seen_at": None,
    }
    # dict.get works for .get() accesses in routes; bracket access for fetchrow results
    class _Row(dict):
        def __getitem__(self, k):
            return dict.__getitem__(self, k)
    return _Row(row)


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


# ── TCDN-AI fixtures ────────────────────────────────────────────────────────
# Kept here (rather than a plugin module) so the AI suite uses the same runner,
# linter and mocking conventions as the rest of the backend. Imports are local:
# the CDN tests must not pay to import the AI package.


@pytest_asyncio.fixture
async def ai_env():
    """Coordinator backed by the in-memory store, one priced model, one key."""
    from types import SimpleNamespace

    from app.ai.config import AISettings
    from app.ai.keyring import KeyRing
    from app.ai.service import Coordinator
    from app.ai.store import MemoryStore, ModelRecord

    settings = AISettings(
        environment="development",
        api_key_pepper="unit-test-pepper-not-a-real-secret",
        coordinator_key_file=None,
        ticket_ttl_seconds=120,
        signup_credits=500,
        rate_limit_per_minute=10_000,
        rate_limit_burst=10_000,
    )
    keyring = KeyRing.generate("coordinator-test")
    store = MemoryStore(settings)
    await store.init()
    model = ModelRecord(
        model_id="llama-3.1-8b-instruct",
        name="Llama 3.1 8B Instruct",
        format="safetensors",
        manifest_hash="a" * 64,
        size_bytes=16 * 1024**3,
        required_vram_gb=8,
        license_note="Llama 3.1 Community License",
        allowlisted=True,
        price_class="small",
        input_micro_per_mtok=3_000_000,
        output_micro_per_mtok=6_000_000,
        effective_from=0,
        history=[(0, 3_000_000, 6_000_000)],
    )
    store.models[model.model_id] = model
    coordinator = Coordinator(store=store, keyring=keyring, settings=settings)
    account_id = "11111111-1111-4111-8111-111111111111"
    api_key = await coordinator.provision_api_key(account_id, scopes=("inference", "worker"))
    await store.credit(
        account_id,
        1_000_000_000,
        source="gateway:test",
        idempotency_key="seed-credits",
        kind="credit_purchase",
    )
    auth = await coordinator.authenticate(api_key)
    return SimpleNamespace(
        settings=settings,
        keyring=keyring,
        store=store,
        coordinator=coordinator,
        model=model,
        account_id=account_id,
        api_key=api_key,
        auth=auth,
    )


@pytest_asyncio.fixture
async def ai_worker_factory(ai_env):
    """Register a worker with the coordinator and return a ready WorkerAgent."""
    import hashlib

    from app.ai import crypto
    from app.ai.keyring import KeyRing
    from app.ai.models import (
        Confidentiality,
        NodeType,
        SignedEnvelope,
        WorkerRegistrationPayload,
    )
    from app.ai.worker import EchoBackend, WorkerAgent, WorkerConfig, WorkerIdentity

    created = []

    async def make(
        *,
        confidentiality: Confidentiality = Confidentiality.NONE,
        attested: bool = False,
        models: list[str] | None = None,
    ):
        identity = WorkerIdentity.generate("pending")
        identity.node_id = "node_" + hashlib.sha256(
            identity.identity_public_key.encode("utf-8")
        ).hexdigest()[:24]
        allowed = models or [ai_env.model.model_id]
        payload = WorkerRegistrationPayload(
            identity_public_key=identity.identity_public_key,
            session_public_key=identity.session_public_key,
            node_type=NodeType.COMPUTE,
            models=allowed,
            gpu_model="NVIDIA H100" if confidentiality is Confidentiality.ATTESTED_TEE else "test-gpu",
            vram_gb=80 if confidentiality is Confidentiality.ATTESTED_TEE else 24,
            region="eu-west",
            price_micro_per_mtok=1_000_000,
            confidentiality=confidentiality,
            software_version="0.1.0",
            endpoint="http://127.0.0.1:9",
        ).model_dump(mode="json")
        envelope = SignedEnvelope(
            payload=payload,
            key_id=identity.identity_key_id,
            sig=crypto.sign("tcdn-ai/node-register/v1", payload, identity.identity_private_key),
        )
        await ai_env.coordinator.register_node(ai_env.auth, envelope)

        trust = KeyRing("coordinator-public")
        active = ai_env.keyring.active
        trust.add_public(active.public_key, key_id=active.key_id)
        agent = WorkerAgent(
            identity=identity,
            config=WorkerConfig(
                models={model: ai_env.model.manifest_hash for model in allowed},
                confidentiality=confidentiality,
                attested=attested,
            ),
            backend=EchoBackend(allow=True),
            coordinator_keys=trust,
            settings=ai_env.settings,
        )
        created.append(agent)
        return agent

    return make


@pytest.fixture(scope="function")
def ai_http(client):
    """Route FastAPI TestClient for the SDK's sync HTTP dependency."""
    return client
