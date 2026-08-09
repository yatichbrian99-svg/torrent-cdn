from pydantic import BaseModel, Field
from typing import Literal, Optional
from uuid import UUID
from datetime import datetime

# Auth input caps. These bound attacker-controlled strings BEFORE they reach
# rate-limit keys (huge emails poison Redis), bcrypt (passwords beyond bcrypt's
# 72-byte input limit are truncated, which can make distinct long passwords
# collide), or VARCHAR columns (a >255-char email/filename otherwise 500s the
# INSERT with a database error). Values are enforced by pydantic (422), so the
# route handlers can assume sane inputs.
MAX_EMAIL_LEN = 255        # users.email is VARCHAR(255)
MAX_PASSWORD_LEN = 72      # bcrypt's hard input limit (bytes)
MAX_TOKEN_LEN = 2048       # JWTs: verify (~200 chars) / reset (~200 chars) + headroom
MAX_FILENAME_LEN = 255     # uploads.filename is VARCHAR(255)
MAX_IDEMPOTENCY_KEY_LEN = 64
MAX_REF_CODE_LEN = 20      # users.referral_code is VARCHAR(20)
MAX_URL_LEN = 2048         # import-by-URL: a URL is a VARCHAR-free request field


class UserInDB(BaseModel):
    id: UUID
    email: str
    password_hash: str
    plan: str = "free"
    stripe_customer_id: Optional[str] = None
    created_at: datetime


class Upload(BaseModel):
    id: UUID
    user_id: UUID
    filename: str
    minio_url: str
    torrent_magnet: str
    file_size_mb: float
    created_at: datetime


class AnalyticsEvent(BaseModel):
    id: UUID
    file_id: UUID
    event_type: str
    bytes_p2p: int = 0
    bytes_origin: int = 0
    created_at: datetime


class UsageRecord(BaseModel):
    id: UUID
    user_id: UUID
    month: str
    total_gb_used: float = 0


# Request/Response models
class UserRegister(BaseModel):
    # MAX caps only — min-length/format rules live in the route handlers so the
    # friendly 400 messages ("Password must be at least 8 characters", "Invalid
    # email address") are preserved instead of becoming generic 422s.
    email: str = Field(max_length=MAX_EMAIL_LEN)
    password: str = Field(max_length=MAX_PASSWORD_LEN)
    terms_accepted: bool = False
    ref_code: Optional[str] = Field(default=None, max_length=MAX_REF_CODE_LEN)


class UserLogin(BaseModel):
    email: str = Field(max_length=MAX_EMAIL_LEN)
    password: str = Field(max_length=MAX_PASSWORD_LEN)


class UserAdminResponse(BaseModel):
    id: UUID
    email: str
    plan: str
    verified: bool = False
    is_admin: bool = False
    created_at: datetime
    usage_gb: float = 0
    file_count: int = 0


class UpdateUserPlanRequest(BaseModel):
    plan: str


class EmailVerifyRequest(BaseModel):
    token: str = Field(max_length=MAX_TOKEN_LEN)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(max_length=MAX_EMAIL_LEN)


class ResetPasswordRequest(BaseModel):
    token: str = Field(max_length=MAX_TOKEN_LEN)
    password: str = Field(max_length=MAX_PASSWORD_LEN)


class AdminAnalyticsResponse(BaseModel):
    total_users: int
    total_files: int
    total_storage_gb: float
    free_users: int
    pro_users: int
    business_users: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MfaStatusResponse(BaseModel):
    """MFA state for the dashboard. `is_admin` drives the recommended-for-admins nudge."""
    enabled: bool
    is_admin: bool = False


class MfaSetupResponse(BaseModel):
    """One-time setup payload. `secret`/`backup_codes` are returned exactly once
    (only the backup-code hashes are stored server-side)."""
    secret: str
    otpauth_uri: str
    backup_codes: list[str]


class MfaCodeRequest(BaseModel):
    """6-digit TOTP code or a 10-char single-use backup code."""
    code: str = Field(min_length=6, max_length=12)


class MfaConfirmRequest(BaseModel):
    """Second factor for a pending MFA login (mfa_token + code)."""
    mfa_token: str = Field(max_length=MAX_TOKEN_LEN)
    code: str = Field(min_length=6, max_length=12)


class ApiKeyResponse(BaseModel):
    """API-key info. `api_key` (the plaintext) is ONLY present on creation — the
    server stores just a hash, so the secret can never be retrieved again."""
    api_key: Optional[str] = None
    prefix: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None


class UploadResponse(BaseModel):
    file_id: UUID
    filename: str
    minio_url: str
    magnet_link: str
    file_size_mb: float
    embed_url: str
    thumbnail_url: Optional[str] = None


class AnalyticsEventCreate(BaseModel):
    file_id: UUID
    event_type: str
    # The event endpoint is public (embed SDK posts unauthenticated), so the
    # byte counters are clamped: negatives and absurd values would otherwise let
    # anyone corrupt views/bandwidth dashboards. 2GB covers the largest legit
    # single event (a full 1GB-file direct fallback reported in one shot).
    bytes_p2p: int = Field(default=0, ge=0, le=2 * 1024**3)
    bytes_origin: int = Field(default=0, ge=0, le=2 * 1024**3)


class FileDetail(BaseModel):
    id: UUID
    filename: str
    minio_url: str
    torrent_magnet: str
    file_size_mb: float
    created_at: datetime
    views: int = 0
    p2p_offload_percent: float = 0
    bandwidth_saved_gb: float = 0
    active_peers: int = 0
    thumbnail_url: Optional[str] = None


class BillingStatus(BaseModel):
    plan: str
    usage_gb: float
    limit_gb: float
    can_upgrade: bool


class CheckoutRequest(BaseModel):
    plan: str


class CheckoutResponse(BaseModel):
    url: str


class UploadSessionRequest(BaseModel):
    """Create a chunked-upload session (client-side chunking fallback through the
    same-origin Vercel proxy — each chunk stays under the proxy's ~4.5MB body cap)."""
    filename: str = Field(max_length=MAX_FILENAME_LEN)
    file_size: int
    total_chunks: int
    content_type: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=MAX_IDEMPOTENCY_KEY_LEN)


class UploadSessionResponse(BaseModel):
    """Result of session creation. If `result` is set the upload already exists
    for this idempotency key — the client returns it without uploading chunks."""
    upload_id: Optional[str] = None
    result: Optional[UploadResponse] = None


class ChunkUploadResponse(BaseModel):
    received: int
    total: int


class UploadPresignRequest(BaseModel):
    """Request a presigned direct-to-storage upload. The browser PUTs the file
    bytes straight to Supabase Storage (never through Render or the Vercel
    proxy); the backend only issues the short-lived URL and later finalizes."""
    filename: str = Field(max_length=MAX_FILENAME_LEN)
    file_size: int
    content_type: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=MAX_IDEMPOTENCY_KEY_LEN)


class UploadPresignResponse(BaseModel):
    """Result of a presign request. If `result` is set the upload already exists
    for this idempotency key — the client returns it without uploading."""
    upload_id: Optional[str] = None
    presigned_url: str = ""
    object_key: str = ""
    content_type: str = ""
    expires_in: int = 0
    result: Optional[UploadResponse] = None


class UploadImportRequest(BaseModel):
    """Import a media file by URL: the server downloads a copy (SSRF-guarded),
    runs the same validation/storage pipeline as every upload path, and
    content-hash dedup applies — identical bytes store once regardless of
    source. `filename` is optional; when absent it is derived from the URL path
    or the response Content-Type."""
    url: str = Field(max_length=MAX_URL_LEN)
    filename: Optional[str] = Field(default=None, max_length=MAX_FILENAME_LEN)
    idempotency_key: Optional[str] = Field(default=None, max_length=MAX_IDEMPOTENCY_KEY_LEN)


# Paystack Transfers API (admin payouts to M-PESA) — see routes/payouts.py.
# Amounts are in the currency's smallest unit (KES cents), matching the
# existing payment integration: KES 500 = amount 50000.


class PayoutRecipientCreate(BaseModel):
    """Create an M-PESA (mobile_money) transfer recipient.

    `account_number` is the recipient's phone number (e.g. 254712345678 or
    0712345678). `type`/`bank_code`/`currency` are pinned to the ONLY supported
    combination (mobile_money / MPESA / KES) — transfers always draw KES from
    the Paystack balance, so a crafted NGN/nuban recipient can never slip in
    and then fail at transfer time.
    """
    name: str = Field(min_length=1, max_length=255)
    account_number: str = Field(min_length=9, max_length=20)
    bank_code: Literal["MPESA"] = "MPESA"
    type: Literal["mobile_money"] = "mobile_money"
    currency: Literal["KES"] = "KES"
    description: Optional[str] = Field(default=None, max_length=255)


class PayoutRecipientResponse(BaseModel):
    """A stored transfer recipient (recipient_code returned by Paystack)."""
    recipient_code: str
    name: str
    account_number: str
    bank_code: str
    bank_name: Optional[str] = None
    type: str = "mobile_money"
    currency: str = "KES"
    active: bool = True
    created_at: datetime


class PayoutInitiateRequest(BaseModel):
    """Initiate a payout from the Paystack balance. `amount` is in whole KES
    (the server converts to cents for the Paystack API, same unit as payments).

    `idempotency_key` (optional) is forwarded to Paystack as the transfer
    `reference` — Paystack dedupes on it, so a retried initiate can never
    double-send. The server also short-circuits a locally-known key and guards
    the insert with a partial unique index on reference.
    """
    recipient_code: str = Field(min_length=1, max_length=64)
    amount: int = Field(gt=0, le=10_000_000)
    reason: Optional[str] = Field(default=None, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class PayoutResponse(BaseModel):
    """A transfer as stored locally (mirrors Paystack's transfer object)."""
    transfer_code: str
    recipient_code: str
    recipient_name: str = ""
    amount_cents: int
    amount_kes: float
    currency: str = "KES"
    reason: Optional[str] = None
    status: str = "pending"
    reference: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class PayoutBalanceResponse(BaseModel):
    """Available Paystack balance for the configured currency (KES)."""
    currency: str
    balance_cents: int
    balance_kes: float
