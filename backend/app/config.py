from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    app_url: str = "http://localhost:3000"

    # Database
    database_url: str = "postgresql://user:password@postgres:5432/torrentcdn"

    # Redis
    redis_url: str = "redis://redis:6379"

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "torrent-cdn-files"
    minio_secure: bool = False
    minio_public_url: str = "http://localhost:9000"

    # JWT
    jwt_secret: str = "changeme"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24 hours

    # Stripe
    stripe_secret_key: str = "sk_test_xxx"
    stripe_webhook_secret: str = "whsec_xxx"
    stripe_price_pro: str = "price_pro_monthly"
    stripe_price_business: str = "price_business_monthly"

    # CSP
    csp_report_url: str = "/api/csp-report"
    csp_report_only: bool = False  # If True, use Content-Security-Policy-Report-Only instead of enforcing

    # CORS
    cors_origins: str = "http://localhost:3000,http://frontend:3000"
    trusted_hosts: str = "localhost,127.0.0.1,frontend"

    # Limits
    max_upload_size_mb: int = 2048
    rate_limit_analytics: int = 60  # max events per minute per IP

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    settings = Settings()
    if settings.app_env == "production":
        if settings.jwt_secret == "changeme" or len(settings.jwt_secret) < 32:
            raise ValueError(
                "JWT_SECRET must be at least 32 characters and not equal to 'changeme' in production"
            )
    return settings
