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
    review = "review"        # credit score 500-649
    approved = "approved"
    rejected = "rejected"    # credit score < 500
    funded = "funded"
    repaid = "repaid"
    defaulted = "defaulted"


class LoanApplicationRequest(BaseModel):
    account_id: str = Field(..., min_length=1, description="Account identifier", examples=["acc_demo_001"])
    customer_id: str = Field(..., min_length=1, description="Customer identifier", examples=["cust_demo_001"])
    amount: float = Field(..., gt=0, description="Loan amount must be positive", examples=[25000.00])
    # ge=300, le=850 enforces the real FICO score range at the door.
    credit_score: int = Field(..., ge=300, le=850, description="Applicant credit score (FICO scale)", examples=[680])
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
    # Pydantic's job here is just confirming `status` is a syntactically real LoanStatus value
    # which `status: LoanStatus` already guarantees on its own.
    status: LoanStatus = Field(..., description="New loan status", examples=["approved"])