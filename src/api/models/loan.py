from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum
import uuid


class LoanType(str, Enum):
    deposit = "deposit"
    withdrawal = "withdrawal"
    transfer = "transfer"


class LoanStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    funded = "funded"
    repaid = "repaid"
    defaulted = "defaulted"


class LoanApplicationRequest(BaseModel):
    account_id: str = Field(..., min_length=1, description="Account identifier", examples=["acc_demo_001"])
    customer_id: str = Field(..., min_length=1, description="Customer identifier", examples=["cust_demo_001"])
    amount: float = Field(..., gt=0, description="Loan amount must be positive", examples=[25000.00])
    type: LoanType = Field(..., description="Transaction type", examples=["deposit"])
    description: Optional[str] = Field(None, max_length=500, examples=["Small business loan application"])

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        return round(v, 2)


class LoanApplicationResponse(BaseModel):
    transaction_id: str
    account_id: str
    customer_id: str
    amount: float
    type: LoanType
    status: LoanStatus
    timestamp: str
    description: Optional[str] = None


class LoanStatusUpdate(BaseModel):
    status: LoanStatus = Field(..., description="New loan status", examples=["approved"])

    @field_validator("status")
    @classmethod
    def validate_transition(cls, v):
        allowed = {LoanStatus.approved, LoanStatus.funded, LoanStatus.repaid, LoanStatus.defaulted}
        if v not in allowed:
            raise ValueError(f"Invalid status transition: {v}")
        return v
