"""TCDN-AI configuration.

Env prefix is ``TCDN_AI_`` so AI settings can never be confused with the CDN's
existing ``JWT_SECRET``/``DATABASE_URL`` family. Every security-relevant default
is the *safe* value; production additionally refuses to boot on an insecure
combination (simulated attestation, dev inference backend, fake payments, burn
enabled without an explicit legal acknowledgement).

The validation is a startup gate, not a warning: a misconfigured deployment
must fail closed rather than serve jobs with weaker guarantees than documented.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

Environment = Literal["development", "staging", "production"]


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TCDN_AI_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "development"

    # Legacy field kept only so existing .env files do not break; it is no longer
    # the feature gate. The real gate is ENABLE_TCDN_AI at startup (default off).
    # Removed in the next cleanup pass once no code reads it.
    enabled: bool = Field(default=True, deprecated=True)

    # ── Revenue split (percent, must sum to exactly 100) ────────────────────
    compute_share_pct: int = Field(default=70, ge=0, le=100)
    seeder_share_pct: int = Field(default=10, ge=0, le=100)
    platform_share_pct: int = Field(default=20, ge=0, le=100)

    # ── Keys and tickets ───────────────────────────────────────────────────
    coordinator_key_file: str | None = None
    # Complete keyring JSON injected by the platform's secret manager at runtime
    # (TCDN_AI_COORDINATOR_KEYRING_JSON). Read-only: the loader never writes it back.
    coordinator_keyring_json: str | None = None
    key_overlap_seconds: int = Field(default=3600, ge=0)
    ticket_ttl_seconds: int = Field(default=120, ge=5, le=900)
    ticket_skew_seconds: int = Field(default=5, ge=0, le=60)
    attestation_verdict_ttl_seconds: int = Field(default=3600, ge=60)
    attestation_nonce_ttl_seconds: int = Field(default=300, ge=30)

    # ── API keys ───────────────────────────────────────────────────────────
    # A pepper only ever comes from the environment/secret store. When absent in
    # development an ephemeral per-process pepper is generated (documented in
    # ASSUMPTIONS.md A7) — keys do not survive a restart, which is acceptable
    # for dev and impossible in production because validation requires it.
    api_key_pepper: str | None = None
    api_key_pepper_id: str = "v1"

    # ── Limits (OWASP API4: unrestricted resource consumption) ─────────────
    max_tokens_per_request: int = Field(default=4096, ge=1, le=32768)
    max_messages: int = Field(default=64, ge=1, le=256)
    max_prompt_chars: int = Field(default=200_000, ge=1)
    max_body_bytes: int = Field(default=262_144, ge=1024)
    max_concurrent_jobs_per_key: int = Field(default=8, ge=1, le=1024)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_burst: int = Field(default=30, ge=1)
    max_worker_concurrency: int = Field(default=4, ge=1, le=256)

    # ── Money ──────────────────────────────────────────────────────────────
    signup_credits: int = Field(default=500, ge=0)
    signup_ip_cap: int = Field(default=3, ge=1)
    confidential_uplift_pct: int = Field(default=25, ge=0, le=1000)
    launch_pricing_until: str = "2027-04-01"
    price_lock_days: int = Field(default=180, ge=0)
    receipt_token_tolerance_pct: int = Field(default=2, ge=0, le=100)
    # ── Credit-hold headroom ────────────────────────────────────────────────
    # The coordinator never sees the prompt (Part 1 invariant), so it prices the
    # reservation from the client's token ESTIMATE. A real engine counts the
    # prompt AFTER applying the model's chat template, which adds tens of tokens
    # the estimate cannot know about (Qwen2.5 added 41 in testing). Without
    # headroom every real-engine job settles above its hold, is disputed, and
    # the customer's credits stay reserved — observed live, see
    # docs/tcdn-ai/evidence/10-real-engine/.
    hold_input_margin_tokens: int = Field(default=64, ge=0, le=8192)
    hold_input_margin_pct: int = Field(default=10, ge=0, le=100)
    verification_sample_pct: int = Field(default=5, ge=0, le=100)
    #: How long a worker keeps encrypted per-token records for challenge
    #: opening. Part 3 §3 default is 24 hours.
    transcript_retention_seconds: int = Field(default=86_400, ge=60, le=7 * 86_400)

    # ── Security posture ───────────────────────────────────────────────────
    require_tls: bool = True
    allow_simulated_attestation: bool = True
    allow_dev_stub_backend: bool = True
    payment_provider: Literal["fake", "paystack"] = "fake"
    enable_tcdn_burn: bool = False
    burn_legal_review_ack: bool = False
    allowed_origin_hosts: tuple[str, ...] = ()

    # A deployment is considered to have the AI environment meaningfully
    # configured when at least one coordindator secret source is present. This is
    # used by the host app to decide whether to mount the /api/ai/v1 routes and
    # run AI_TABLES_DDL when APP_ENV=production.
    @property
    def has_secret_source(self) -> bool:
        return bool(
            self.coordinator_key_file
            or self.coordinator_keyring_json
            or self.api_key_pepper
        )

    # ── P2P ────────────────────────────────────────────────────────────────
    max_model_bytes: int = Field(default=400 * 1024**3, ge=1)
    max_shard_bytes: int = Field(default=256 * 1024**2, ge=1)
    shard_fetch_concurrency: int = Field(default=8, ge=1, le=64)
    shard_fetch_attempts: int = Field(default=3, ge=1, le=10)
    seeder_quota_bytes: int = Field(default=200 * 1024**3, ge=1)
    seeder_payout_delay_seconds: int = Field(default=86400, ge=0)
    seeder_pair_cap_bytes: int = Field(default=50 * 1024**3, ge=1)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _validate(self) -> AISettings:
        total = self.compute_share_pct + self.seeder_share_pct + self.platform_share_pct
        if total != 100:
            raise ValueError(
                "compute/seeder/platform share percentages must sum to exactly 100 "
                f"(got {total})"
            )
        if self.is_production:
            problems: list[str] = []
            if not self.require_tls:
                problems.append("TCDN_AI_REQUIRE_TLS must be true in production")
            if self.allow_simulated_attestation:
                problems.append(
                    "TCDN_AI_ALLOW_SIMULATED_ATTESTATION must be false in production"
                )
            if self.allow_dev_stub_backend:
                problems.append(
                    "TCDN_AI_ALLOW_DEV_STUB_BACKEND must be false in production"
                )
            if self.payment_provider == "fake":
                problems.append(
                    "TCDN_AI_PAYMENT_PROVIDER must not be 'fake' in production"
                )
            if not self.api_key_pepper:
                problems.append("TCDN_AI_API_KEY_PEPPER must be set in production")
            if not self.coordinator_key_file and not self.coordinator_keyring_json:
                problems.append(
                    "TCDN_AI_COORDINATOR_KEY_FILE or TCDN_AI_COORDINATOR_KEYRING_JSON must "
                    "supply the coordinator keyring in production"
                )
            if self.enable_tcdn_burn and not self.burn_legal_review_ack:
                problems.append(
                    "TCDN_AI_ENABLE_TCDN_BURN requires TCDN_AI_BURN_LEGAL_REVIEW_ACK=true "
                    "(legal/tax review) in production"
                )
            if problems:
                raise ValueError(
                    "insecure TCDN-AI production config: " + "; ".join(problems)
                )
        return self

    def is_ai_enabled_for_host(self, host_env: str) -> bool:
        """Feature-flag decision used by the host app (main.py).

        The gate is ENABLE_TCDN_AI (default off). When off, the host app does not
        mount the /api/ai/v1 routes or run AI_TABLES_DDL.

        When the host app is in production, we also refuse to enable the AI routes
        unless the AI environment is actually configured: if TCDN_AI_ENVIRONMENT is
        absent or still development, the host app treats that as a misconfigured
        production deployment and keeps AI routes off rather than silently falling
        back to development defaults.
        """
        raw = os.environ.get("ENABLE_TCDN_AI", "").strip().lower()
        flag_on = raw in {"1", "true", "yes"}
        if not flag_on:
            return False
        if host_env == "production":
            if self.environment != "production":
                return False
            if not self.has_secret_source:
                return False
        return True

    def pepper_bytes(self) -> bytes:
        """API-key pepper. Dev-only ephemeral fallback, production enforced above."""
        if self.api_key_pepper:
            return self.api_key_pepper.encode("utf-8")
        if self.is_production:  # pragma: no cover - validator already rejects
            raise RuntimeError("api key pepper missing in production")
        pepper = os.urandom(32).hex()
        logger.warning(
            "TCDN_AI_API_KEY_PEPPER unset — generated an ephemeral development "
            "pepper; API keys will not survive a restart"
        )
        return pepper.encode("utf-8")


@lru_cache
def get_ai_settings() -> AISettings:
    return AISettings()
