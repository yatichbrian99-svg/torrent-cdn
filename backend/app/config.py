from pydantic_settings import BaseSettings
from functools import lru_cache
from urllib.parse import urlparse
import logging
import os


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

    # Stripe
    stripe_secret_key: str = "sk_test_xxx"
    stripe_webhook_secret: str = "whsec_xxx"
    stripe_price_pro: str = "price_pro_monthly"
    stripe_price_business: str = "price_business_monthly"

    # Resend (transactional email)
    resend_api_key: str = ""

    # CSP
    csp_report_url: str = "/csp-report"
    csp_report_only: bool = False  # If True, use Content-Security-Policy-Report-Only instead of enforcing

    # CORS
    cors_origins: str = "http://localhost:3000,http://frontend:3000"
    trusted_hosts: str = "localhost,127.0.0.1,frontend,*.onrender.com,*.vercel.app"

    # Limits
    max_upload_size_mb: int = 1024  # 1 GB max file size (reduced from 2GB)
    rate_limit_analytics: int = 60  # max events per minute per IP
    rate_limit_global: int = 5      # max requests per window per IP
    rate_limit_window_seconds: int = 10  # sliding window for global rate limit

    # Email verification
    # In production, users must verify their email before they can log in.
    # In development, this is disabled so you don't need a working email service.
    email_verification_required: bool = False  # overridden by env var

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
        logger = logging.getLogger(__name__)
        # Warn loudly (but don't crash) if placeholder credentials are detected
        if settings.minio_secret_key == "minioadmin":
            logger.warning(
                "MINIO_SECRET_KEY is the insecure default 'minioadmin' — set a real secret in production"
            )
        if settings.stripe_secret_key in ("sk_test_xxx", "sk_live_xxx"):
            logger.warning(
                "STRIPE_SECRET_KEY looks like a placeholder — set a real Stripe secret key in production"
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
