from pydantic import BaseModel
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


class AnalyticsEventCreate(BaseModel):
    file_id: UUID
    event_type: str
    bytes_p2p: int = 0
    bytes_origin: int = 0


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


class BillingStatus(BaseModel):
    plan: str
    usage_gb: float
    limit_gb: float
    can_upgrade: bool


class CheckoutRequest(BaseModel):
    plan: str


class CheckoutResponse(BaseModel):
    url: str
