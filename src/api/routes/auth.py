import os
from fastapi import APIRouter, Request
from fastapi.security import HTTPBearer
from src.api.models.user import (
    UserRegisterRequest,
    UserLoginRequest,
    TokenResponse,
    UserResponse
)
from src.api.core import auth_repository

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
- At least one uppercase letter (A - Z)
— At least one lowercase letter (a - z)
- At least one number (1 - 9)
— At least one special character (e.g., !@#$%^&*)

**Demo account for testing:**
Use `demo@fintech.raynercodes.dev` / `Demo1234!` to get a JWT token via `/auth/login`.

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """
)
async def register(request: UserRegisterRequest):
    return await auth_repository.register(request)


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

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """
)
async def login(request: UserLoginRequest, req: Request):
    source_ip = req.client.host
    return await auth_repository.login(request, source_ip)


@router.post(
    "/demo",
    response_model=TokenResponse,
    summary="Get demo JWT token",
    description="""
Returns a JWT token for the demo account — no registration required.

Use this to instantly test all protected endpoints from the Swagger UI.

**This endpoint is for employer and reviewer testing only.**

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """
)
async def demo_login():
    return await auth_repository.demo_login()


@router.get("/verify", include_in_schema=False)
async def verify_login(token: str):
    return await auth_repository.verify_login(token)
