from pydantic import BaseModel, Field, EmailStr, field_validator
import re


class UserRegisterRequest(BaseModel):
    email: EmailStr = Field(..., description="User email — used as login identifier")
    password: str = Field(..., min_length=8, description="Minimum 8 characters")
    account_id: str = Field(..., min_length=1, description="Business account identifier")
    customer_id: str = Field(..., min_length=1, description="Customer identifier")

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v):
        # Enforce minimum complexity — not just length
        # Real fintech systems require this.
        # This security pattern must be enforced
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one number")
        return v


class UserLoginRequest(BaseModel):
    email: EmailStr = Field(..., description="Registered email")
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    account_id: str
    customer_id: str


class UserResponse(BaseModel):
    email: str
    account_id: str
    customer_id: str
    created_at: str
