import re
from fastapi import APIRouter, Depends, HTTPException, status
from uuid import UUID

from ..database import get_db_pool, get_redis
from ..auth import hash_password, verify_password, create_access_token, get_current_user_id
from ..models import UserRegister, UserLogin, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(data: UserRegister):
    # Validate email format
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', data.email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Validate minimum password length
    if len(data.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1", data.email
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

        password_hash = hash_password(data.password)
        user = await conn.fetchrow(
            """INSERT INTO users (email, password_hash)
               VALUES ($1, $2)
               RETURNING id""",
            data.email,
            password_hash,
        )

        token = create_access_token({"sub": str(user["id"])})
        return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin):
    pool = await get_db_pool()
    redis = await get_redis()

    # Rate limiting: check login attempts
    rate_key = f"login_attempts:{data.email}"
    attempts = await redis.get(rate_key)
    if attempts and int(attempts) >= 5:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again in 15 minutes.",
        )

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, password_hash FROM users WHERE email = $1",
            data.email,
        )
        if not user or not verify_password(data.password, user["password_hash"]):
            # Increment failed attempt counter
            await redis.incr(rate_key)
            await redis.expire(rate_key, 900)  # 15 minutes
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

    # Reset rate limit on success
    await redis.delete(rate_key)

    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token)


@router.get("/me")
async def get_me(user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, plan, created_at FROM users WHERE id = $1",
            user_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(user)
