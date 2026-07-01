from pydantic import BaseModel, Field, EmailStr, field_validator
import re


class UserRegisterRequest(BaseModel):
    email: EmailStr = Field(..., description="User email — used as login identifier", examples=["your@email.com"])
    password: str = Field(
        ...,
        min_length=8,
        description="Minimum 8 characters, one uppercase, one lowercase, one number, one special character",
        examples=["YourPass1!"]
    )
    account_id: str = Field(..., min_length=1, description="Business account identifier", examples=["acc_your_001"])
    customer_id: str = Field(..., min_length=1, description="Customer identifier", examples=["cust_your_001"])

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v):
        # Enforce minimum complexity — not just length
        # Real fintech systems require this.
        # This security pattern must be enforced
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[0-9]", v):
            raise ValueError("Password must contain at least one number")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class UserLoginRequest(BaseModel):
    email: EmailStr = Field(..., description="Registered email", examples=["your@email.com"])
    password: str = Field(..., min_length=1, examples=["YourPass1!"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: str
    account_id: str
    customer_id: str


class UserResponse(BaseModel):
    email: str
    account_id: str
    customer_id: str
    created_at: str
