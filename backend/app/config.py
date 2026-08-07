from pydantic_settings import BaseSettings
from functools import lru_cache
from urllib.parse import urlparse
import json
import logging
import os
import time
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    app_url: str = "http://localhost:3000"
    # Custom domain for production (e.g. https://torrentcdn.com)

    # Database
    # For Supabase: postgresql://postgres.xxxxx:password@aws-0-xxx.pooler.supabase.com:6543/postgres
    database_url: str = "postgresql://user:password@postgres:5432/torrentcdn"

    # Redis
    # For Upstash/Redis Cloud, set the full redis:// URL
    redis_url: str = "redis://redis:6379"

    # S3-compatible Storage (MinIO for local, Supabase Storage / AWS S3 / Backblaze B2 for production)
    # For Supabase Storage: endpoint is project-ref.supabase.co/storage/v1/s3
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "torrent-cdn-files"
    minio_secure: bool = False
    # Public URL for direct file access (e.g., Supabase Storage: https://project-ref.supabase.co/storage/v1/object/public/bucket/
    # MinIO public URL (should include bucket path for both local and Supabase)
    # Local: http://localhost:9000/torrent-cdn-files
    # Supabase: https://project-ref.supabase.co/storage/v1/object/public/torrent-cdn-files
    minio_public_url: str = "http://localhost:9000/torrent-cdn-files"
    # Region for S3-compatible storage (required by some providers like AWS S3)
    minio_region: str = "us-east-1"

    # JWT — Authentication tokens
    jwt_secret: str = "changeme"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    # Separate secret for email verification tokens (different from auth JWT)
    jwt_verify_secret: str = ""
    # Separate secret for password reset tokens
    jwt_reset_secret: str = ""

    # Admin bootstrap: when True, /auth/setup can promote users to admin.
    # Set ADMIN_SETUP_ENABLED=true temporarily to create the first admin, then disable it.
    admin_setup_enabled: bool = False

    # API docs (Swagger UI / ReDoc / OpenAPI schema). Off in production by
    # default — the public schema hands attackers a precise route map (OWASP
    # API3:2019). Set ENABLE_API_DOCS=true to force them on in production.
    api_docs_enabled: bool = False

    # Paystack (Kenya — Stripe does not support merchant accounts registered
    # in Kenya). Same secret key signs both API calls AND webhooks
    # (x-paystack-signature, HMAC-SHA512); there is no separate webhook secret.
    # Plan amounts are in the currency's smallest unit (KES cents), so
    # 400000 = KES 4,000/month.
    paystack_secret_key: str = "sk_test_xxx"
    paystack_currency: str = "KES"
    paystack_plan_pro: str = "PLN_pro_monthly"
    paystack_plan_business: str = "PLN_business_monthly"
    paystack_amount_pro: int = 400000
    paystack_amount_business: int = 1200000

    # Resend (transactional email)
    resend_api_key: str = ""

    # CSP
    csp_report_url: str = "/csp-report"
    csp_report_only: bool = False  # If True, use Content-Security-Policy-Report-Only instead of enforcing

    # CORS
    cors_origins: str = "http://localhost:3000,http://frontend:3000"
    trusted_hosts: str = "localhost,127.0.0.1,frontend,*.onrender.com,*.vercel.app"

    # Limits
    # 2GB — the app's own ceiling for a single file. Supabase Storage free tier
    # hard-caps objects at 50MB regardless of this value (the 50MB cap is a
    # plan limit, not ours); on a plan/provider without a per-file cap (e.g.
    # Cloudflare R2, Backblaze B2, Supabase Pro) this is the real limit.
    max_upload_size_mb: int = 2048
    rate_limit_analytics: int = 60  # max events per minute per IP
    # Global per-IP request budget — a BURST CAP, not the primary flood gate.
    # 30/10s (3 req/s sustained) — the dashboard fires 6 concurrent authed GETs
    # on mount (me/referral/api-key/verification/bandwidth/files), so a budget
    # below that self-tripped every page load on the 6th call. The real
    # anti-abuse weight lives in the per-route limiters (login lockout,
    # register, uploads/hour, analytics/min, public-file/min); this one only
    # stops gross bursts (a browser page does ~1-2 bursts of <10).
    rate_limit_global: int = 30     # max requests per window per IP
    rate_limit_window_seconds: int = 10  # sliding window for global rate limit
    # Per-IP upload throttling (F-05 hardening): max upload requests per IP per
    # window. Protects storage/bandwidth from bulk-automated upload abuse — the
    # global limiter only paces request rate, not cumulative uploads.
    rate_limit_uploads: int = 10           # max uploads per IP per window
    rate_limit_uploads_window: int = 3600  # window in seconds (1 hour)

    # Orphaned-object sweep cadence (production background task that deletes
    # storage objects PUT but never finalized). Env: ORPHAN_SWEEP_INTERVAL_SECONDS.
    orphan_sweep_interval_seconds: int = 900  # 15 min

    # Clock-skew guard (production)
    # JWT exp (email verify 24h, password reset 1h) is minted and validated
    # against the local clock, so a badly drifted instance silently shortens or
    # lengthens token lifetimes. Fail loudly at startup if the app clock is more
    # than this many minutes off a public NTP reference (env: CLOCK_SKEW_MAX_MINUTES).
    clock_skew_max_minutes: int = 5

    # Email verification
    # In production, users must verify their email before they can log in.
    # In development, this is disabled so you don't need a working email service.
    email_verification_required: bool = False  # overridden by env var

    # Two-factor authentication (TOTP, stdlib — see app/mfa.py)
    # Lifetime of the password-half MFA step token minted at login. Keep short:
    # the user must present their authenticator code before it expires.
    mfa_expire_minutes: int = 5

    # Opt-in malware scanning (see app/malware_scan.py). Without a key the
    # feature is OFF and every upload is treated as clean (fail-open). With a
    # key, finalized uploads are checked and definitively-malicious files are
    # rejected (422 + object deleted). VirusTotal free tier uploads ≤32MB.
    virustotal_api_key: str = ""
    virustotal_max_upload_mb: int = 32

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    # Fail closed: never allow the default/placeholder JWT secret, in ANY environment.
    # An attacker who can forge JWTs can impersonate any user, including admins.
    if settings.jwt_secret == "changeme" or len(settings.jwt_secret) < 32:
        raise ValueError(
            "JWT_SECRET must be at least 32 characters and not equal to 'changeme'"
        )
    # Auto-generate verification/reset secrets if not explicitly set
    if not settings.jwt_verify_secret:
        import hashlib
        settings.jwt_verify_secret = hashlib.sha256(
            (settings.jwt_secret + "__verify__").encode()
        ).hexdigest()
    if not settings.jwt_reset_secret:
        import hashlib
        settings.jwt_reset_secret = hashlib.sha256(
            (settings.jwt_secret + "__reset__").encode()
        ).hexdigest()
    if settings.app_env == "production":
        # Warn loudly (but don't crash) if placeholder credentials are detected
        if settings.minio_secret_key == "minioadmin":
            logger.warning(
                "MINIO_SECRET_KEY is the insecure default 'minioadmin' — set a real secret in production"
            )
        if settings.paystack_secret_key in ("sk_test_xxx", "sk_live_xxx"):
            logger.warning(
                "PAYSTACK_SECRET_KEY looks like a placeholder — set a real Paystack secret key in production"
            )

        # APP_URL drives every user-facing link the backend emits (email
        # verification, password reset, referral URLs, billing redirects, embed
        # URLs, and the CSP media/connect-src). A stale value silently breaks
        # email verification: the link points at a dead domain and users can
        # never verify. Fail loudly instead of shipping a broken link.
        parsed = urlparse(settings.app_url)
        app_host = (parsed.hostname or "").lower()
        app_scheme = parsed.scheme.lower()  # urllib.parse already lowercases, kept for clarity
        if app_host in ("localhost", "127.0.0.1"):
            logger.error(
                "APP_URL points at localhost (%s) in production — email verification/reset "
                "links will point at localhost. Set APP_URL to your real frontend domain.",
                settings.app_url,
            )
        elif app_host in ("torrentcdn.com", "www.torrentcdn.com"):
            # The apex torrentcdn.com no longer resolves (confirmed dead DNS).
            # The canonical frontend domain is www.torrentcdn.store. Correct it
            # for this process and tell the operator to fix the env var.
            logger.warning(
                "APP_URL (%s) points at the dead torrentcdn.com domain — overriding to "
                "https://www.torrentcdn.store for this process. Update APP_URL in the Render "
                "dashboard to https://www.torrentcdn.store and redeploy so the fix is permanent.",
                settings.app_url,
            )
            settings.app_url = "https://www.torrentcdn.store"
        elif not app_scheme or not app_host:
            # No scheme or no hostname (e.g. empty, "torrentcdn.store", "http://") —
            # links built from it are unresolvable. Fail loudly, never ship silently.
            logger.error(
                "APP_URL (%r) has no usable scheme/hostname in production — email "
                "verification/reset links will be broken. Set APP_URL to your real "
                "frontend domain, e.g. https://www.torrentcdn.store.",
                settings.app_url,
            )
        elif app_scheme == "http":
            # Plain http:// in production: HSTS makes browsers block http links,
            # so email verification/reset would still appear to "go nowhere".
            # Upgrade the scheme for this process and tell the operator.
            upgraded = "https://" + settings.app_url.split("://", 1)[1]
            logger.warning(
                "APP_URL uses http:// — upgraded to %s for this process. Set APP_URL "
                "to an https:// URL in the Render dashboard.",
                upgraded,
            )
            settings.app_url = upgraded

        # In production, email verification is required by default
        # (unless explicitly disabled via env var EMAIL_VERIFICATION_REQUIRED=false)
        if not os.environ.get("EMAIL_VERIFICATION_REQUIRED"):
            settings.email_verification_required = True
    return settings


def _fetch_reference_epoch(timeout: float = 5.0) -> Optional[float]:
    """Best-effort fetch of an authoritative UTC epoch from a public NTP-over-HTTP source.

    Tries several reliable endpoints in order; returns the first usable epoch or
    None if every source is unreachable/malformed. Never raises — the clock
    guard must be fail-open (an NTP outage must not block or crash startup).
    """
    # 1) Cloudflare trace endpoint — plain `ts=<unix epoch>` line, no JSON
    try:
        with urllib.request.urlopen(
            "https://www.cloudflare.com/cdn-cgi/trace", timeout=timeout
        ) as resp:
            for line in resp.read().decode("utf-8", "replace").splitlines():
                if line.startswith("ts="):
                    return float(line.split("=", 1)[1])
    except Exception:
        pass

    # 2) HTTP Date header from a reliable CDN edge (NTP-synced). HTTP dates are
    #    naive UTC — pin tzinfo to UTC before .timestamp(), otherwise it is
    #    interpreted as local time and the skew reading is garbage.
    try:
        req = urllib.request.Request("https://www.google.com/", method="HEAD")
        # Some CDN edges reject requests without a User-Agent (403/400) — set one
        # so the primary reference source is reliable in production.
        req.add_header("User-Agent", "torrent-cdn-clock-guard/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            date_hdr = resp.headers.get("Date")
            if date_hdr:
                dt = parsedate_to_datetime(date_hdr)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
    except Exception:
        pass

    # 3) worldtimeapi JSON fallback
    try:
        with urllib.request.urlopen(
            "https://worldtimeapi.org/api/timezone/Etc/UTC", timeout=timeout
        ) as resp:
            data = json.load(resp)
            ut = data.get("unixtime")
            if ut:
                return float(ut)
    except Exception:
        pass

    return None


def check_clock_skew(settings: Optional[Settings] = None) -> None:
    """Startup clock-skew guard (production only, fail-open).

    Compares the local clock against a public NTP-over-HTTP reference and logs
    an ERROR if the app clock deviates by more than ``clock_skew_max_minutes``.
    JWT ``exp`` (email verify 24h, password reset 1h) is minted/validated against
    the local clock, so a badly drifted instance silently breaks token lifetimes
    — fail loudly instead. Never raises: an unreachable reference logs a warning
    and the process continues.
    """
    s = settings or get_settings()
    if s.app_env != "production":
        return

    threshold_seconds = s.clock_skew_max_minutes * 60
    try:
        ref_epoch = _fetch_reference_epoch()
        if ref_epoch is None:
            logger.warning(
                "Clock skew check skipped — no NTP reference reachable (fail-open)"
            )
            return
        skew_seconds = abs(time.time() - ref_epoch)
        if skew_seconds > threshold_seconds:
            logger.error(
                "CLOCK SKEW: app clock is %d seconds off real time (threshold %ds) — "
                "JWT expiries (email verify 24h, password reset 1h) will be wrong. "
                "Check instance time / NTP sync.",
                int(skew_seconds),
                threshold_seconds,
            )
        else:
            logger.info(
                "Clock skew check OK: app clock within %ds of real time",
                int(skew_seconds),
            )
    except Exception as e:
        logger.warning("Clock skew check skipped — error: %s", e)
