from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID
from datetime import datetime


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
    email: str
    password: str
    terms_accepted: bool = False
    ref_code: Optional[str] = None


class UserLogin(BaseModel):
    email: str
    password: str


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
    token: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


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
    filename: str
    file_size: int
    total_chunks: int
    content_type: Optional[str] = None
    idempotency_key: Optional[str] = None


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
    filename: str
    file_size: int
    content_type: Optional[str] = None
    idempotency_key: Optional[str] = None


class UploadPresignResponse(BaseModel):
    """Result of a presign request. If `result` is set the upload already exists
    for this idempotency key — the client returns it without uploading."""
    upload_id: Optional[str] = None
    presigned_url: str = ""
    object_key: str = ""
    content_type: str = ""
    expires_in: int = 0
    result: Optional[UploadResponse] = None
