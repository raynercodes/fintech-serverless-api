import os
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer
from src.api.models.user import (
    UserRegisterRequest,
    UserLoginRequest,
    TokenResponse,
    UserResponse
)
from src.api.core.security import (
    hash_password,
    verify_password,
    create_jwt
)
from src.api.core.users_db import (
    get_user_by_email,
    create_user
)

router = APIRouter()
bearer_scheme = HTTPBearer()

# Demo account credentials — hardcoded for employer testing
# Allows anyone hitting the /docs page to test protected endpoints
# without needing to register a real account
# These are created on first registration — not hardcoded in the DB
DEMO_EMAIL = "demo@fintech.raynercodes.dev"
DEMO_PASSWORD = "Demo1234!"
DEMO_ACCOUNT_ID = "acc_demo_001"
DEMO_CUSTOMER_ID = "cust_demo_001"


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=201,
    summary="Register a new user",
    description="""
Create a new user account.

**Password requirements:**
- Minimum 8 characters
- At least one uppercase letter
- At least one number

**Demo account for testing:**
Use `demo@fintech.raynercodes.dev` / `Demo1234!` to get a JWT token via `/auth/login`.
    """
)
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


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login and receive JWT",
    description="""
Authenticate with email and password to receive a JWT access token.

Token expires in **15 minutes**.

**Demo credentials:**
- Email: `demo@fintech.raynercodes.dev`
- Password: `Demo1234!`

**How to use the token:**
1. Copy the `access_token` from the response
2. Click **Authorize** at the top of the page
3. Paste the token in the `Value` field
4. Click **Authorize** then **Close**
5. All protected endpoints will now include your token automatically
    """
)
async def login(request: UserLoginRequest):
    # Fetch user by email — O(1) DynamoDB get_item on partition key
    user = get_user_by_email(request.email)

    if user is None:
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
        expires_in=900,
        account_id=user["account_id"],
        customer_id=user["customer_id"]
    )


@router.post(
    "/demo",
    response_model=TokenResponse,
    summary="Get demo JWT token",
    description="""
Returns a JWT token for the demo account — no registration required.

Use this to instantly test all protected endpoints from the Swagger UI.

**This endpoint is for employer and reviewer testing only.**
    """
)
async def demo_login():
    # Check if demo account exists — create it if not
    # Self-healing demo account — always available regardless of DB state
    demo_user = get_user_by_email(DEMO_EMAIL)

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
        expires_in=900,
        account_id=DEMO_ACCOUNT_ID,
        customer_id=DEMO_CUSTOMER_ID
    )
