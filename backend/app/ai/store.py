"""Persistence for the coordinator.

Two implementations of the same ``Store`` protocol:

* ``PostgresStore`` — production. Parameterised SQL only; money-affecting writes
  run inside a transaction, and holds take a per-account advisory lock so two
  concurrent placements cannot overdraw the same balance.
* ``MemoryStore`` — tests and local development **only**. It refuses to be
  constructed when ``TCDN_AI_ENVIRONMENT=production`` and is documented as a
  test double, never a silent fallback.

DDL is additive and idempotent, matching the repo's existing startup-migration
style (``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE ... IF NOT EXISTS``).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AISettings
from .errors import ConflictError, InsufficientCredits, NotFoundError, ValidationError
from .ledger import (
    PLATFORM_REVENUE,
    SEEDER_POOL,
    Ledger,
    Split,
    account_credits,
    compute_earnings,
    split_amounts,
)

AI_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS ai_accounts (
    account_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    price_lock_until BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ai_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES ai_accounts(account_id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    prefix VARCHAR(24) NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT '{inference}',
    pepper_id VARCHAR(16) NOT NULL DEFAULT 'v1',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_ai_api_keys_account ON ai_api_keys(account_id);

CREATE TABLE IF NOT EXISTS ai_nodes (
    node_id VARCHAR(64) PRIMARY KEY,
    owner_id UUID,
    identity_public_key TEXT NOT NULL,
    identity_key_id VARCHAR(64) NOT NULL,
    session_public_key TEXT NOT NULL,
    node_type VARCHAR(16) NOT NULL DEFAULT 'compute',
    models TEXT[] NOT NULL DEFAULT '{}',
    gpu_model VARCHAR(64) NOT NULL DEFAULT 'unknown',
    vram_gb INTEGER NOT NULL DEFAULT 0,
    region VARCHAR(64) NOT NULL DEFAULT 'unknown',
    price_micro_per_mtok BIGINT NOT NULL DEFAULT 0,
    confidentiality VARCHAR(16) NOT NULL DEFAULT 'none',
    attestation_verdict VARCHAR(16),
    attestation_expires_at BIGINT,
    attestation_ref TEXT,
    reputation INTEGER NOT NULL DEFAULT 50,
    load_percent INTEGER NOT NULL DEFAULT 0,
    latency_ms_p95 INTEGER NOT NULL DEFAULT 200,
    healthy BOOLEAN NOT NULL DEFAULT TRUE,
    suspended BOOLEAN NOT NULL DEFAULT FALSE,
    software_version VARCHAR(32),
    endpoint TEXT,
    last_heartbeat BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_jobs (
    job_id VARCHAR(64) PRIMARY KEY,
    account_id UUID NOT NULL,
    node_id VARCHAR(64),
    model VARCHAR(128) NOT NULL,
    manifest_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    tier VARCHAR(16) NOT NULL,
    confidentiality VARCHAR(16) NOT NULL,
    max_tokens INTEGER NOT NULL,
    max_price_micro BIGINT,
    prompt_tokens_estimate INTEGER NOT NULL DEFAULT 0,
    ticket_hash CHAR(64),
    hold_micro BIGINT NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    total_micro BIGINT NOT NULL DEFAULT 0,
    compute_micro BIGINT NOT NULL DEFAULT 0,
    seeder_micro BIGINT NOT NULL DEFAULT 0,
    platform_micro BIGINT NOT NULL DEFAULT 0,
    price_effective_from BIGINT,
    input_micro_per_mtok BIGINT,
    output_micro_per_mtok BIGINT,
    split_compute_pct INTEGER,
    split_seeder_pct INTEGER,
    split_platform_pct INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    settled_at TIMESTAMP WITH TIME ZONE,
    dispute_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_account ON ai_jobs(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status);

CREATE TABLE IF NOT EXISTS ai_holds (
    job_id VARCHAR(64) PRIMARY KEY,
    account_id UUID NOT NULL,
    amount_micro BIGINT NOT NULL CHECK (amount_micro > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_holds_account_status ON ai_holds(account_id, status);

CREATE TABLE IF NOT EXISTS ai_ledger_entries (
    entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    debit_account TEXT NOT NULL,
    credit_account TEXT NOT NULL,
    amount_micro BIGINT NOT NULL CHECK (amount_micro > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    kind VARCHAR(32) NOT NULL,
    job_id VARCHAR(64),
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_ledger_accounts ON ai_ledger_entries(debit_account, credit_account);

CREATE TABLE IF NOT EXISTS ai_receipts (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    kind VARCHAR(8) NOT NULL CHECK (kind IN ('worker', 'client')),
    node_id VARCHAR(64),
    body JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (job_id, kind)
);

CREATE TABLE IF NOT EXISTS ai_attestations (
    id BIGSERIAL PRIMARY KEY,
    node_id VARCHAR(64) NOT NULL,
    platform VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL,
    verifier VARCHAR(32) NOT NULL,
    session_key_sha256 CHAR(64) NOT NULL,
    evidence_hash CHAR(64) NOT NULL,
    verdict_hash CHAR(64),
    issued_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ai_attestations_node ON ai_attestations(node_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_models (
    model_id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    format VARCHAR(32) NOT NULL,
    manifest_hash CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    required_vram_gb INTEGER NOT NULL,
    license_note TEXT,
    allowlisted BOOLEAN NOT NULL DEFAULT FALSE,
    price_class VARCHAR(16) NOT NULL DEFAULT 'small',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ai_model_prices (
    id BIGSERIAL PRIMARY KEY,
    model_id VARCHAR(128) NOT NULL REFERENCES ai_models(model_id) ON DELETE CASCADE,
    input_micro_per_mtok BIGINT NOT NULL,
    output_micro_per_mtok BIGINT NOT NULL,
    effective_from BIGINT NOT NULL,
    launch_pricing BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (model_id, effective_from)
);

CREATE TABLE IF NOT EXISTS ai_transfer_receipts (
    id BIGSERIAL PRIMARY KEY,
    downloader_id VARCHAR(64) NOT NULL,
    seeder_id VARCHAR(64) NOT NULL,
    shard_sha256 CHAR(64) NOT NULL,
    bytes BIGINT NOT NULL CHECK (bytes > 0),
    downloader_signature TEXT NOT NULL,
    seeder_signature TEXT NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (downloader_id, seeder_id, shard_sha256, bytes)
);

CREATE TABLE IF NOT EXISTS ai_payouts (
    id BIGSERIAL PRIMARY KEY,
    period_start BIGINT NOT NULL,
    period_end BIGINT NOT NULL,
    node_id VARCHAR(64) NOT NULL,
    component VARCHAR(16) NOT NULL,
    amount_micro BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (period_start, period_end, node_id, component)
);

CREATE TABLE IF NOT EXISTS ai_signup_grants (
    account_id UUID PRIMARY KEY,
    ip_hash TEXT NOT NULL,
    granted_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_signup_grants_ip ON ai_signup_grants(ip_hash);

CREATE TABLE IF NOT EXISTS ai_audit_log (
    id BIGSERIAL PRIMARY KEY,
    event VARCHAR(64) NOT NULL,
    actor VARCHAR(128),
    subject VARCHAR(128),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""


# ── records ─────────────────────────────────────────────────────────────────


@dataclass
class ApiKeyRecord:
    account_id: str
    key_hash: str
    prefix: str
    scopes: tuple[str, ...] = ("inference",)
    pepper_id: str = "v1"
    status: str = "active"
    created_at: int = 0
    last_used_at: int | None = None


@dataclass
class NodeRecord:
    node_id: str
    owner_id: str | None
    identity_public_key: str
    identity_key_id: str
    session_public_key: str
    node_type: str = "compute"
    models: list[str] = field(default_factory=list)
    gpu_model: str = "unknown"
    vram_gb: int = 0
    region: str = "unknown"
    price_micro_per_mtok: int = 0
    confidentiality: str = "none"
    attestation_verdict: str | None = None
    attestation_expires_at: int | None = None
    attestation_ref: str | None = None
    reputation: int = 50
    load: float = 0.0
    latency_ms_p95: int = 200
    healthy: bool = True
    suspended: bool = False
    software_version: str = "0.1.0"
    endpoint: str = ""
    last_heartbeat: int | None = None

    @property
    def key_attestation_ref(self) -> str | None:
        return self.attestation_ref


@dataclass
class JobRecord:
    job_id: str
    account_id: str
    node_id: str
    model: str
    manifest_hash: str
    status: str
    tier: str
    confidentiality: str
    max_tokens: int
    max_price_micro: int
    prompt_tokens_estimate: int
    hold_micro: int
    ticket_hash: str
    price_effective_from: int
    split: Split
    input_micro_per_mtok: int = 0
    output_micro_per_mtok: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_micro: int = 0
    compute_micro: int = 0
    seeder_micro: int = 0
    platform_micro: int = 0
    created_at: int = 0
    settled_at: int | None = None
    dispute_reason: str | None = None
    receipt: dict[str, Any] | None = None
    ack: dict[str, Any] | None = None


@dataclass
class ModelRecord:
    model_id: str
    name: str
    format: str
    manifest_hash: str
    size_bytes: int
    required_vram_gb: int
    license_note: str
    allowlisted: bool
    price_class: str
    input_micro_per_mtok: int
    output_micro_per_mtok: int
    effective_from: int = 0
    launch_pricing: bool = True
    history: list[tuple[int, int, int]] = field(default_factory=list)


class Store(Protocol):
    async def init(self) -> None: ...

    async def create_api_key(self, record: ApiKeyRecord) -> None: ...

    async def get_api_key(self, key_hash: str) -> ApiKeyRecord | None: ...

    async def touch_api_key(self, key_hash: str, at: int) -> None: ...

    async def register_node(self, node: NodeRecord) -> NodeRecord: ...

    async def get_node(self, node_id: str) -> NodeRecord | None: ...

    async def list_nodes(self) -> list[NodeRecord]: ...

    async def account_price_lock(self, account_id: str) -> int | None: ...

    async def update_node_heartbeat(
        self, node_id: str, *, load: float, healthy: bool, at: int, endpoint: str = ""
    ) -> None: ...

    async def penalize_node(
        self, node_id: str, *, reputation_delta: int, suspend: bool = False
    ) -> None: ...

    async def save_attestation(self, node_id: str, **kwargs: Any) -> None: ...

    async def create_job(self, job: JobRecord) -> None: ...

    async def get_job(self, job_id: str) -> JobRecord | None: ...

    async def list_jobs(self, account_id: str, limit: int = 100) -> list[JobRecord]: ...

    async def place_hold(
        self, job_id: str, account_id: str, amount_micro: int
    ) -> None: ...

    async def release_hold(self, job_id: str) -> None: ...

    async def save_receipt(
        self, job_id: str, kind: str, node_id: str, body: dict
    ) -> None: ...

    async def get_receipt(self, job_id: str) -> dict | None: ...

    async def get_ack(self, job_id: str) -> dict | None: ...

    async def settle_job(
        self,
        job: JobRecord,
        *,
        input_tokens: int,
        output_tokens: int,
        total_micro: int,
        dispute_reason: str | None,
    ) -> JobRecord: ...

    async def mark_job(self, job_id: str, **fields: Any) -> None: ...

    async def expire_stale_jobs(self, ttl_seconds: int) -> list[str]: ...

    async def balance_micro(self, account_id: str) -> int: ...

    async def held_micro(self, account_id: str) -> int: ...

    async def credit(
        self,
        account_id: str,
        amount_micro: int,
        *,
        source: str,
        idempotency_key: str,
        kind: str,
    ) -> None: ...

    async def list_models(self) -> list[ModelRecord]: ...

    async def get_model(self, model_id: str) -> ModelRecord | None: ...

    async def record_signup_grant(self, account_id: str, ip_hash: str) -> bool: ...

    async def count_grants_for_ip(self, ip_hash: str) -> int: ...

    async def save_transfer_receipt(self, record: dict[str, Any]) -> None: ...

    async def transfer_receipts(self, since: int) -> list[dict[str, Any]]: ...

    async def save_payout(self, record: dict[str, Any]) -> None: ...

    async def audit(
        self,
        event: str,
        *,
        actor: str = "",
        subject: str = "",
        detail: dict | None = None,
    ) -> None: ...


# ── memory implementation ───────────────────────────────────────────────────


class MemoryStore:
    """Test/dev store. Refuses to exist in production."""

    def __init__(self, settings: AISettings, *, clock=time.time) -> None:
        if settings.is_production:
            raise RuntimeError("MemoryStore is not available in production")
        self.settings = settings
        self._clock = clock
        self.ledger = Ledger(clock=lambda: int(self._clock()))
        self.api_keys: dict[str, ApiKeyRecord] = {}
        self.nodes: dict[str, NodeRecord] = {}
        self.jobs: dict[str, JobRecord] = {}
        self.receipts: dict[tuple[str, str], dict] = {}
        self.models: dict[str, ModelRecord] = {}
        self.signup_grants: dict[str, str] = {}
        self.transfers: list[dict] = []
        self.payouts: list[dict] = []
        self.audit_log: list[dict] = []
        self.attestations: list[dict] = []
        self.price_locks: dict[str, int] = {}

    async def init(self) -> None:
        return None

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        self.api_keys[record.key_hash] = record

    async def get_api_key(self, key_hash: str) -> ApiKeyRecord | None:
        return self.api_keys.get(key_hash)

    async def touch_api_key(self, key_hash: str, at: int) -> None:
        record = self.api_keys.get(key_hash)
        if record is not None:
            record.last_used_at = at

    async def register_node(self, node: NodeRecord) -> NodeRecord:
        node.last_heartbeat = int(self._clock())
        self.nodes[node.node_id] = node
        return node

    async def get_node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    async def list_nodes(self) -> list[NodeRecord]:
        return list(self.nodes.values())

    async def account_price_lock(self, account_id: str) -> int | None:
        return self.price_locks.get(account_id)

    async def update_node_heartbeat(
        self, node_id: str, *, load: float, healthy: bool, at: int, endpoint: str = ""
    ) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            raise NotFoundError("unknown node")
        node.load = max(0.0, min(1.0, load))
        node.healthy = healthy
        node.last_heartbeat = at
        if endpoint:
            node.endpoint = endpoint

    async def penalize_node(
        self, node_id: str, *, reputation_delta: int, suspend: bool = False
    ) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            raise NotFoundError("unknown node")
        node.reputation = max(0, min(100, node.reputation + reputation_delta))
        if suspend:
            node.suspended = True

    async def save_attestation(self, node_id: str, **kwargs: Any) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            raise NotFoundError("unknown node")
        node.attestation_verdict = kwargs.get("status")
        node.attestation_expires_at = kwargs.get("expires_at")
        node.attestation_ref = kwargs.get("verdict_hash")
        self.attestations.append({"node_id": node_id, **kwargs})

    async def create_job(self, job: JobRecord) -> None:
        self.jobs[job.job_id] = job

    async def get_job(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    async def list_jobs(self, account_id: str, limit: int = 100) -> list[JobRecord]:
        jobs = [j for j in self.jobs.values() if j.account_id == account_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    async def place_hold(self, job_id: str, account_id: str, amount_micro: int) -> None:
        self.ledger.place_hold(
            job_id=job_id,
            account=account_credits(account_id),
            amount_micro=amount_micro,
        )

    async def release_hold(self, job_id: str) -> None:
        self.ledger.release_hold(job_id)

    async def expire_stale_jobs(self, ttl_seconds: int) -> list[str]:
        cutoff = int(self._clock()) - ttl_seconds
        expired: list[str] = []
        for job in list(self.jobs.values()):
            if job.status == "placed" and job.created_at < cutoff:
                job.status = "expired"
                self.ledger.release_hold(job.job_id)
                expired.append(job.job_id)
        return expired

    async def save_receipt(
        self, job_id: str, kind: str, node_id: str, body: dict
    ) -> None:
        key = (job_id, kind)
        if key in self.receipts:
            raise ConflictError(
                "receipt already submitted for this job", code="duplicate_receipt"
            )
        self.receipts[key] = {
            "job_id": job_id,
            "kind": kind,
            "node_id": node_id,
            "body": body,
        }

    async def get_receipt(self, job_id: str) -> dict | None:
        record = self.receipts.get((job_id, "worker"))
        return record["body"] if record else None

    async def get_ack(self, job_id: str) -> dict | None:
        record = self.receipts.get((job_id, "client"))
        return record["body"] if record else None

    async def settle_job(
        self,
        job: JobRecord,
        *,
        input_tokens: int,
        output_tokens: int,
        total_micro: int,
        dispute_reason: str | None,
    ) -> JobRecord:
        if job.status == "settled":
            return job
        if dispute_reason is not None:
            job.status = "disputed"
            job.dispute_reason = dispute_reason
            job.input_tokens = input_tokens
            job.output_tokens = output_tokens
            return job
        settlement = self.ledger.settle(
            job_id=job.job_id,
            total_micro=total_micro,
            split=job.split,
            compute_account=compute_earnings(job.node_id),
            seeder_account=SEEDER_POOL,
            platform_account=PLATFORM_REVENUE,
            at=int(self._clock()),
        )
        job.status = "settled"
        job.input_tokens = input_tokens
        job.output_tokens = output_tokens
        job.total_micro = settlement.total_micro
        job.compute_micro = settlement.compute_micro
        job.seeder_micro = settlement.seeder_micro
        job.platform_micro = settlement.platform_micro
        job.settled_at = int(self._clock())
        return job

    async def mark_job(self, job_id: str, **fields: Any) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFoundError("unknown job")
        for key, value in fields.items():
            if not hasattr(job, key):
                raise ValidationError(f"unknown job field: {key}")
            setattr(job, key, value)

    async def balance_micro(self, account_id: str) -> int:
        return self.ledger.balance(account_credits(account_id))

    async def held_micro(self, account_id: str) -> int:
        return self.ledger.held(account_credits(account_id))

    async def credit(
        self,
        account_id: str,
        amount_micro: int,
        *,
        source: str,
        idempotency_key: str,
        kind: str,
    ) -> None:
        self.ledger.deposit(
            account=account_credits(account_id),
            amount_micro=amount_micro,
            source=source,
            idempotency_key=idempotency_key,
            kind=kind,
            at=int(self._clock()),
        )

    async def list_models(self) -> list[ModelRecord]:
        return list(self.models.values())

    async def get_model(self, model_id: str) -> ModelRecord | None:
        return self.models.get(model_id)

    async def record_signup_grant(self, account_id: str, ip_hash: str) -> bool:
        if account_id in self.signup_grants:
            return False
        self.signup_grants[account_id] = ip_hash
        return True

    async def count_grants_for_ip(self, ip_hash: str) -> int:
        return sum(1 for value in self.signup_grants.values() if value == ip_hash)

    async def save_transfer_receipt(self, record: dict[str, Any]) -> None:
        self.transfers.append(record)

    async def transfer_receipts(self, since: int) -> list[dict[str, Any]]:
        return [r for r in self.transfers if r.get("created_at", 0) >= since]

    async def save_payout(self, record: dict[str, Any]) -> None:
        self.payouts.append(record)

    async def audit(
        self,
        event: str,
        *,
        actor: str = "",
        subject: str = "",
        detail: dict | None = None,
    ) -> None:
        self.audit_log.append(
            {"event": event, "actor": actor, "subject": subject, "detail": detail or {}}
        )


# ── postgres implementation ─────────────────────────────────────────────────


def _row_to_node(row: Any) -> NodeRecord:
    return NodeRecord(
        node_id=row["node_id"],
        owner_id=str(row["owner_id"]) if row.get("owner_id") else None,
        identity_public_key=row["identity_public_key"],
        identity_key_id=row["identity_key_id"],
        session_public_key=row["session_public_key"],
        node_type=row["node_type"],
        models=list(row["models"] or []),
        gpu_model=row["gpu_model"],
        vram_gb=int(row["vram_gb"] or 0),
        region=row["region"],
        price_micro_per_mtok=int(row["price_micro_per_mtok"] or 0),
        confidentiality=row["confidentiality"],
        attestation_verdict=row["attestation_verdict"],
        attestation_expires_at=row["attestation_expires_at"],
        attestation_ref=row["attestation_ref"],
        reputation=int(row["reputation"] or 50),
        load=float(row["load_percent"] or 0) / 100.0,
        latency_ms_p95=int(row["latency_ms_p95"] or 200),
        healthy=bool(row["healthy"]),
        suspended=bool(row["suspended"]),
        software_version=row["software_version"] or "0.1.0",
        endpoint=row["endpoint"] or "",
        last_heartbeat=row["last_heartbeat"],
    )


def _row_to_job(row: Any) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        account_id=str(row["account_id"]),
        node_id=row["node_id"] or "",
        model=row["model"],
        manifest_hash=row["manifest_hash"],
        status=row["status"],
        tier=row["tier"],
        confidentiality=row["confidentiality"],
        max_tokens=int(row["max_tokens"]),
        max_price_micro=int(row["max_price_micro"] or 0),
        prompt_tokens_estimate=int(row["prompt_tokens_estimate"] or 0),
        hold_micro=int(row["hold_micro"] or 0),
        ticket_hash=row["ticket_hash"] or "",
        price_effective_from=int(row["price_effective_from"] or 0),
        input_micro_per_mtok=int(row["input_micro_per_mtok"] or 0),
        output_micro_per_mtok=int(row["output_micro_per_mtok"] or 0),
        split=Split(
            compute_pct=int(row["split_compute_pct"]),
            seeder_pct=int(row["split_seeder_pct"]),
            platform_pct=int(row["split_platform_pct"]),
        ),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        total_micro=int(row["total_micro"] or 0),
        compute_micro=int(row["compute_micro"] or 0),
        seeder_micro=int(row["seeder_micro"] or 0),
        platform_micro=int(row["platform_micro"] or 0),
        # created_at is a timestamptz in Postgres but an epoch int in
        # JobRecord. Leaving it at the default 0 made every job look older
        # than the TTL: the coordinator rejected every worker receipt as
        # "job has expired", so no job on Postgres could ever settle.
        created_at=(
            int(row["created_at"].timestamp()) if row["created_at"] is not None else 0
        ),
        settled_at=row["settled_at"],
        dispute_reason=row["dispute_reason"],
    )


class PostgresStore:
    """Production store. All SQL is parameterised; no string interpolation."""

    def __init__(self, pool: Any, settings: AISettings) -> None:
        self.pool = pool
        self.settings = settings

    async def init(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(AI_TABLES_DDL)

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_accounts (account_id) VALUES ($1) ON CONFLICT DO NOTHING",
                _uuid(record.account_id),
            )
            await conn.execute(
                """
                INSERT INTO ai_api_keys (account_id, key_hash, prefix, scopes, pepper_id, status)
                VALUES ($1, $2, $3, $4, $5, 'active')
                """,
                _uuid(record.account_id),
                record.key_hash,
                record.prefix,
                list(record.scopes),
                record.pepper_id,
            )

    async def get_api_key(self, key_hash: str) -> ApiKeyRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_api_keys WHERE key_hash = $1 AND status = 'active'",
                key_hash,
            )
        if row is None:
            return None
        return ApiKeyRecord(
            account_id=str(row["account_id"]),
            key_hash=row["key_hash"],
            prefix=row["prefix"],
            scopes=tuple(row["scopes"] or ()),
            pepper_id=row["pepper_id"],
            status=row["status"],
        )

    async def touch_api_key(self, key_hash: str, at: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE ai_api_keys SET last_used_at = NOW() WHERE key_hash = $1",
                key_hash,
            )

    async def register_node(self, node: NodeRecord) -> NodeRecord:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_nodes (
                    node_id, owner_id, identity_public_key, identity_key_id,
                    session_public_key, node_type, models, gpu_model, vram_gb, region,
                    price_micro_per_mtok, confidentiality, software_version, endpoint,
                    last_heartbeat
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                          EXTRACT(EPOCH FROM NOW())::BIGINT)
                ON CONFLICT (node_id) DO UPDATE SET
                    identity_public_key = EXCLUDED.identity_public_key,
                    identity_key_id = EXCLUDED.identity_key_id,
                    session_public_key = EXCLUDED.session_public_key,
                    node_type = EXCLUDED.node_type,
                    models = EXCLUDED.models,
                    gpu_model = EXCLUDED.gpu_model,
                    vram_gb = EXCLUDED.vram_gb,
                    region = EXCLUDED.region,
                    price_micro_per_mtok = EXCLUDED.price_micro_per_mtok,
                    confidentiality = EXCLUDED.confidentiality,
                    software_version = EXCLUDED.software_version,
                    endpoint = EXCLUDED.endpoint,
                    -- Registering is proof of life: a node with no heartbeat yet
                    -- must still be schedulable for its first job.
                    last_heartbeat = EXCLUDED.last_heartbeat
                """,
                node.node_id,
                _uuid(node.owner_id) if node.owner_id else None,
                node.identity_public_key,
                node.identity_key_id,
                node.session_public_key,
                node.node_type,
                node.models,
                node.gpu_model,
                node.vram_gb,
                node.region,
                node.price_micro_per_mtok,
                node.confidentiality,
                node.software_version,
                node.endpoint,
            )
        return node

    async def get_node(self, node_id: str) -> NodeRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_nodes WHERE node_id = $1", node_id
            )
        return _row_to_node(row) if row else None

    async def list_nodes(self) -> list[NodeRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM ai_nodes")
        return [_row_to_node(row) for row in rows]

    async def account_price_lock(self, account_id: str) -> int | None:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT price_lock_until FROM ai_accounts WHERE account_id = $1",
                _uuid(account_id),
            )
        return int(value) if value is not None else None

    async def update_node_heartbeat(
        self, node_id: str, *, load: float, healthy: bool, at: int, endpoint: str = ""
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ai_nodes SET load_percent = $2, healthy = $3,
                    last_heartbeat = $4, endpoint = COALESCE(NULLIF($5, ''), endpoint)
                WHERE node_id = $1
                """,
                node_id,
                int(max(0.0, min(1.0, load)) * 100),
                healthy,
                at,
                endpoint,
            )

    async def penalize_node(
        self, node_id: str, *, reputation_delta: int, suspend: bool = False
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ai_nodes SET
                    reputation = GREATEST(0, LEAST(100, reputation + $2)),
                    suspended = suspended OR $3
                WHERE node_id = $1
                """,
                node_id,
                reputation_delta,
                suspend,
            )

    async def save_attestation(self, node_id: str, **kwargs: Any) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO ai_attestations (
                        node_id, platform, status, verifier, session_key_sha256,
                        evidence_hash, verdict_hash, issued_at, expires_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    """,
                node_id,
                kwargs["platform"],
                kwargs["status"],
                kwargs["verifier"],
                kwargs["session_key_sha256"],
                kwargs["evidence_hash"],
                kwargs.get("verdict_hash"),
                kwargs["issued_at"],
                kwargs["expires_at"],
            )
            await conn.execute(
                """
                    UPDATE ai_nodes SET attestation_verdict = $2,
                        attestation_expires_at = $3, attestation_ref = $4
                    WHERE node_id = $1
                    """,
                node_id,
                kwargs["status"],
                kwargs["expires_at"],
                kwargs.get("verdict_hash"),
            )

    async def create_job(self, job: JobRecord) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, account_id, node_id, model, manifest_hash, status, tier,
                    confidentiality, max_tokens, max_price_micro, prompt_tokens_estimate,
                    ticket_hash, hold_micro, price_effective_from,
                    input_micro_per_mtok, output_micro_per_mtok,
                    split_compute_pct, split_seeder_pct, split_platform_pct
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                """,
                job.job_id,
                _uuid(job.account_id),
                job.node_id,
                job.model,
                job.manifest_hash,
                job.status,
                job.tier,
                job.confidentiality,
                job.max_tokens,
                job.max_price_micro,
                job.prompt_tokens_estimate,
                job.ticket_hash,
                job.hold_micro,
                job.price_effective_from,
                job.input_micro_per_mtok,
                job.output_micro_per_mtok,
                job.split.compute_pct,
                job.split.seeder_pct,
                job.split.platform_pct,
            )

    async def get_job(self, job_id: str) -> JobRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ai_jobs WHERE job_id = $1", job_id)
        return _row_to_job(row) if row else None

    async def list_jobs(self, account_id: str, limit: int = 100) -> list[JobRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_jobs WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2",
                _uuid(account_id),
                limit,
            )
        return [_row_to_job(row) for row in rows]

    async def place_hold(self, job_id: str, account_id: str, amount_micro: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            # Serialise per account so two concurrent placements cannot both
            # see the same available balance.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", account_id)
            available = await conn.fetchval(
                """
                    SELECT COALESCE((
                        SELECT SUM(CASE WHEN credit_account = $1 THEN amount_micro
                                        ELSE -amount_micro END)
                        FROM ai_ledger_entries
                        WHERE $1 IN (debit_account, credit_account)
                    ), 0) - COALESCE((
                        SELECT SUM(amount_micro) FROM ai_holds
                        WHERE account_id = $2 AND status = 'open'
                    ), 0)
                    """,
                account_credits(account_id),
                _uuid(account_id),
            )
            if int(available or 0) < amount_micro:
                raise InsufficientCredits(
                    "insufficient credit balance for this job",
                    details={
                        "available_micro": max(0, int(available or 0)),
                        "required_micro": amount_micro,
                    },
                )
            await conn.execute(
                "INSERT INTO ai_holds (job_id, account_id, amount_micro) VALUES ($1,$2,$3)",
                job_id,
                _uuid(account_id),
                amount_micro,
            )

    async def release_hold(self, job_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE ai_holds SET status = 'released' WHERE job_id = $1 AND status = 'open'",
                job_id,
            )

    async def expire_stale_jobs(self, ttl_seconds: int) -> list[str]:
        """Expire jobs whose worker never reported, and free their holds.

        Without the hold release a crashed worker locks the customer's credits
        forever: the job is terminal, but the money stays reserved.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                """
                WITH stale AS (
                    SELECT job_id FROM ai_jobs
                    WHERE status = 'placed'
                      AND created_at < NOW() - make_interval(secs => $1)
                    FOR UPDATE SKIP LOCKED
                ), updated AS (
                    UPDATE ai_jobs SET status = 'expired', updated_at = NOW()
                    WHERE job_id IN (SELECT job_id FROM stale)
                    RETURNING job_id
                ), released AS (
                    UPDATE ai_holds SET status = 'released'
                    WHERE job_id IN (SELECT job_id FROM updated) AND status = 'open'
                    RETURNING job_id
                )
                SELECT job_id FROM updated
                """,
                int(ttl_seconds),
            )
        return [str(row["job_id"]) for row in rows]

    async def save_receipt(
        self, job_id: str, kind: str, node_id: str, body: dict
    ) -> None:
        async with self.pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO ai_receipts (job_id, kind, node_id, body)
                    VALUES ($1,$2,$3,$4::jsonb)
                    """,
                    job_id,
                    kind,
                    node_id,
                    json.dumps(body),
                )
            except Exception as exc:  # unique violation
                raise ConflictError(
                    "receipt already submitted for this job", code="duplicate_receipt"
                ) from exc

    async def get_receipt(self, job_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body FROM ai_receipts WHERE job_id = $1 AND kind = 'worker'",
                job_id,
            )
        return json.loads(row["body"]) if row else None

    async def get_ack(self, job_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body FROM ai_receipts WHERE job_id = $1 AND kind = 'client'",
                job_id,
            )
        return json.loads(row["body"]) if row else None

    async def settle_job(
        self,
        job: JobRecord,
        *,
        input_tokens: int,
        output_tokens: int,
        total_micro: int,
        dispute_reason: str | None,
    ) -> JobRecord:
        if job.status == "settled":
            return job
        if dispute_reason is not None:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE ai_jobs SET status = 'disputed', dispute_reason = $2,
                        input_tokens = $3, output_tokens = $4, updated_at = NOW()
                    WHERE job_id = $1
                    """,
                    job.job_id,
                    dispute_reason,
                    input_tokens,
                    output_tokens,
                )
            job.status = "disputed"
            job.dispute_reason = dispute_reason
            return job

        amounts = split_amounts(total_micro, job.split)
        compute = amounts.compute_micro
        seeder = amounts.seeder_micro
        platform = amounts.platform_micro
        entries = [
            (
                account_credits(job.account_id),
                compute_earnings(job.node_id),
                compute,
                "job_compute",
            ),
            (account_credits(job.account_id), SEEDER_POOL, seeder, "job_seeder"),
            (
                account_credits(job.account_id),
                PLATFORM_REVENUE,
                platform,
                "job_platform",
            ),
        ]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", job.account_id
            )
            for debit, credit, amount, kind in entries:
                if amount <= 0:
                    continue
                await conn.execute(
                    """
                        INSERT INTO ai_ledger_entries (
                            debit_account, credit_account, amount_micro,
                            idempotency_key, kind, job_id, created_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                    debit,
                    credit,
                    amount,
                    f"settle:{job.job_id}:{kind}",
                    kind,
                    job.job_id,
                    int(time.time()),
                )
            await conn.execute(
                "UPDATE ai_holds SET status = 'settled' WHERE job_id = $1", job.job_id
            )
            await conn.execute(
                """
                    UPDATE ai_jobs SET status = 'settled', input_tokens = $2,
                        output_tokens = $3, total_micro = $4, compute_micro = $5,
                        seeder_micro = $6, platform_micro = $7, settled_at = NOW(),
                        updated_at = NOW()
                    WHERE job_id = $1
                    """,
                job.job_id,
                input_tokens,
                output_tokens,
                total_micro,
                compute,
                seeder,
                platform,
            )
        job.status = "settled"
        job.input_tokens = input_tokens
        job.output_tokens = output_tokens
        job.total_micro = total_micro
        job.compute_micro = compute
        job.seeder_micro = seeder
        job.platform_micro = platform
        job.settled_at = int(time.time())
        return job

    async def mark_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "node_id", "ticket_hash"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"unknown job fields: {sorted(unknown)}")
        # Static SQL with COALESCE: no identifier is ever interpolated.
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ai_jobs SET
                    status = COALESCE($2, status),
                    node_id = COALESCE($3, node_id),
                    ticket_hash = COALESCE($4, ticket_hash),
                    updated_at = NOW()
                WHERE job_id = $1
                """,
                job_id,
                fields.get("status"),
                fields.get("node_id"),
                fields.get("ticket_hash"),
            )

    async def balance_micro(self, account_id: str) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(CASE WHEN credit_account = $1 THEN amount_micro
                                         ELSE -amount_micro END), 0)
                FROM ai_ledger_entries
                WHERE $1 IN (debit_account, credit_account)
                """,
                account_credits(account_id),
            )
        return int(value or 0)

    async def held_micro(self, account_id: str) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_micro), 0) FROM ai_holds "
                "WHERE account_id = $1 AND status = 'open'",
                _uuid(account_id),
            )
        return int(value or 0)

    async def credit(
        self,
        account_id: str,
        amount_micro: int,
        *,
        source: str,
        idempotency_key: str,
        kind: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_ledger_entries (
                    debit_account, credit_account, amount_micro, idempotency_key, kind, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                source,
                account_credits(account_id),
                amount_micro,
                idempotency_key,
                kind,
                int(time.time()),
            )

    async def list_models(self) -> list[ModelRecord]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.*, p.input_micro_per_mtok, p.output_micro_per_mtok,
                       p.effective_from, p.launch_pricing
                FROM ai_models m
                JOIN LATERAL (
                    SELECT * FROM ai_model_prices p
                    WHERE p.model_id = m.model_id ORDER BY p.effective_from DESC LIMIT 1
                ) p ON TRUE
                """
            )
        return [_row_to_model(row) for row in rows]

    async def get_model(self, model_id: str) -> ModelRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT m.*, p.input_micro_per_mtok, p.output_micro_per_mtok,
                       p.effective_from, p.launch_pricing
                FROM ai_models m
                JOIN LATERAL (
                    SELECT * FROM ai_model_prices p
                    WHERE p.model_id = m.model_id ORDER BY p.effective_from DESC LIMIT 1
                ) p ON TRUE
                WHERE m.model_id = $1
                """,
                model_id,
            )
            if row is None:
                return None
            history = await conn.fetch(
                """
                SELECT effective_from, input_micro_per_mtok, output_micro_per_mtok
                FROM ai_model_prices WHERE model_id = $1 ORDER BY effective_from
                """,
                model_id,
            )
        return _row_to_model(row, history)

    async def record_signup_grant(self, account_id: str, ip_hash: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO ai_signup_grants (account_id, ip_hash, granted_at)
                VALUES ($1,$2,$3) ON CONFLICT (account_id) DO NOTHING
                """,
                _uuid(account_id),
                ip_hash,
                int(time.time()),
            )
        return result.endswith("1")

    async def count_grants_for_ip(self, ip_hash: str) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM ai_signup_grants WHERE ip_hash = $1", ip_hash
            )
        return int(value or 0)

    async def save_transfer_receipt(self, record: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_transfer_receipts (
                    downloader_id, seeder_id, shard_sha256, bytes,
                    downloader_signature, seeder_signature, verified
                ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (downloader_id, seeder_id, shard_sha256, bytes) DO NOTHING
                """,
                record["downloader_id"],
                record["seeder_id"],
                record["shard_sha256"],
                record["bytes"],
                record["downloader_signature"],
                record["seeder_signature"],
                record["verified"],
            )

    async def transfer_receipts(self, since: int) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ai_transfer_receipts WHERE created_at >= to_timestamp($1)",
                since,
            )
        return [dict(row) for row in rows]

    async def save_payout(self, record: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_payouts (
                    period_start, period_end, node_id, component, amount_micro, status
                ) VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (period_start, period_end, node_id, component) DO NOTHING
                """,
                record["period_start"],
                record["period_end"],
                record["node_id"],
                record["component"],
                record["amount_micro"],
                record.get("status", "pending"),
            )

    async def audit(
        self,
        event: str,
        *,
        actor: str = "",
        subject: str = "",
        detail: dict | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ai_audit_log (event, actor, subject, detail) VALUES ($1,$2,$3,$4::jsonb)",
                event,
                actor,
                subject,
                json.dumps(detail or {}),
            )


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value) if not isinstance(value, uuid.UUID) else value
    except (ValueError, AttributeError) as exc:
        raise ValidationError("account id must be a UUID") from exc


def _row_to_model(row: Any, history: Any = None) -> ModelRecord:
    return ModelRecord(
        model_id=row["model_id"],
        name=row["name"],
        format=row["format"],
        manifest_hash=row["manifest_hash"],
        size_bytes=int(row["size_bytes"]),
        required_vram_gb=int(row["required_vram_gb"]),
        license_note=row["license_note"] or "",
        allowlisted=bool(row["allowlisted"]),
        price_class=row["price_class"],
        input_micro_per_mtok=int(row["input_micro_per_mtok"]),
        output_micro_per_mtok=int(row["output_micro_per_mtok"]),
        effective_from=int(row["effective_from"]),
        launch_pricing=bool(row["launch_pricing"]),
        history=[
            (
                int(item["effective_from"]),
                int(item["input_micro_per_mtok"]),
                int(item["output_micro_per_mtok"]),
            )
            for item in (history or [])
        ],
    )
