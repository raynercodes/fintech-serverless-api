from datetime import datetime, timedelta
from fastapi import HTTPException
from src.api.core.security import hash_password, verify_password, create_jwt
from src.api.core.users_db import get_user_by_email, create_user
from src.api.models.user import UserRegisterRequest, UserLoginRequest, TokenResponse, UserResponse
from src.api.core.login_lockout import (
    check_login_lockout,
    record_failed_login,
    clear_login_attempts,
    find_email_by_verification_token,
    clear_verification_requirement
)

# Demo account credentials — business data, not a routing concern,
# so it belongs here alongside the logic that actually uses it
DEMO_EMAIL = "demo@fintech.raynercodes.dev"
DEMO_PASSWORD = "Demo1234!"
DEMO_ACCOUNT_ID = "acc_demo_001"
DEMO_CUSTOMER_ID = "cust_demo_001"


# ============================================================
# 1. POST /register
# ============================================================
async def register(request: UserRegisterRequest):
    # Check if user already exists before hashing
    # Saves 600,000 PBKDF2 iterations if email is already taken
    # never makes it to the validation step — saves CPU time and Secrets Manager calls
    # Small optimization for my portfolio but meaningful at scale
    existing_user = get_user_by_email(request.email)
    if existing_user:
        # Generic message — don't confirm email exists
        # Same uniform error response philosophy as login
        raise HTTPException(
            status_code=409,
            detail="Registration unsuccessful"
        )

    # Hash password with salt + pepper
    # 600,000 PBKDF2-HMAC-SHA256 iterations
    # Pepper fetched from Secrets Manager, never stored in DB
    password_hash = hash_password(request.password)

    # Create user — conditional write prevents race condition
    # if two registration requests hit simultaneously
    user = create_user(
        email=request.email,
        password_hash=password_hash,
        account_id=request.account_id,
        customer_id=request.customer_id
    )

    if user is None:
        raise HTTPException(
            status_code=409,
            detail="Registration unsuccessful"
        )

    return UserResponse(
        email=user["email"],
        account_id=user["account_id"],
        customer_id=user["customer_id"],
        created_at=user["created_at"]
    )


# ============================================================
# 2. POST /login
# ============================================================
async def login(request: UserLoginRequest, source_ip: str) -> TokenResponse:
    if check_login_lockout(request.email, source_ip):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Fetch user by email — O(1) DynamoDB get_item on partition key
    user = get_user_by_email(request.email)
    JWT_EXP_TIME = datetime.now() + timedelta(seconds=900)

    if user is None:
        # record the failed attempt of a ip logging in with unregistered emails
        record_failed_login(request.email, source_ip)
        # User not found — same error as wrong password
        # Uniform response prevents email enumeration
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    # Verify password against stored hash using pepper from Secrets Manager
    # 600,000 PBKDF2 iterations — deliberately slow to resist brute force
    password_valid = verify_password(request.password, user["password_hash"])

    if not password_valid:
        # same thing as the check in email, make sure the hacker isn't able to brute force the password
        record_failed_login(request.email, source_ip)
        # Wrong password — same error as user not found
        # Attacker cannot distinguish between the two failure modes
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )

    # Check account is still active — soft deletes land here
    # if somehow the field is missing from an old record
    # defaults to active rather than locking everyone out — safe default
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials"
        )
    
    clear_login_attempts(request.email, source_ip)

    # Issue JWT — 15 minute expiry
    # Short window limits damage if token is stolen
    # Paired with re-login flow rather than refresh tokens
    # for simplicity at portfolio scale
    access_token = create_jwt(
        account_id=user["account_id"],
        customer_id=user["customer_id"],
        expires_in_seconds=900
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=f"15 minutes — Timestamp: {JWT_EXP_TIME.strftime('%A, %Y-%m-%d %I:%M:%S %p')}",
        account_id=user["account_id"],
        customer_id=user["customer_id"]
    )


# ============================================================
# 3. POST /demo
# ============================================================
async def demo_login() -> TokenResponse:
    # Check if demo account exists — create it if not
    # Self-healing demo account — always available regardless of DB state
    demo_user = get_user_by_email(DEMO_EMAIL)
    JWT_EXP_TIME = datetime.now() + timedelta(seconds=900)

    if demo_user is None:
        # Auto-create demo account on first hit
        password_hash = hash_password(DEMO_PASSWORD)
        demo_user = create_user(
            email=DEMO_EMAIL,
            password_hash=password_hash,
            account_id=DEMO_ACCOUNT_ID,
            customer_id=DEMO_CUSTOMER_ID
        )

        if demo_user is None:
            # Race condition — another request created it simultaneously
            # Fetch the one that was just created
            demo_user = get_user_by_email(DEMO_EMAIL)

    # Issue JWT for demo account
    access_token = create_jwt(
        account_id=DEMO_ACCOUNT_ID,
        customer_id=DEMO_CUSTOMER_ID,
        expires_in_seconds=900
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=f"15 minutes — Timestamp: {JWT_EXP_TIME.strftime('%A, %Y-%m-%d %I:%M:%S %p')}",
        account_id=DEMO_ACCOUNT_ID,
        customer_id=DEMO_CUSTOMER_ID
    )


async def verify_login(token: str) -> dict:
    email = find_email_by_verification_token(token)
    if email is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    clear_verification_requirement(email)
    return {"message": "Identity verified. You may now attempt to log in again."}
