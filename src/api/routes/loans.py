import json
import uuid
import os
import boto3
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request, Depends, Path
from fastapi.security import HTTPBearer
from boto3.dynamodb.conditions import Key
from src.api.models.loan import (
    LoanApplicationRequest,
    LoanApplicationResponse,
    LoanStatusUpdate,
    LoanStatus
)
from src.api.core.database import get_transactions_table
from src.api.core.cache import cache_get, cache_set, cache_delete
from src.api.core.idempotency import compute_content_hash, claim_duplicate_check, get_duplicate_transaction_id
from src.api.core import loans_repository # this imports the MODULE, not the functions directly
from src.api.core.security import verify_jwt

router = APIRouter()
bearer_scheme = HTTPBearer()



@router.post(
        "/",
        response_model=LoanApplicationResponse,
        status_code=201,
        summary="Submit Loan Application",
        description="""
Submit a new loan application for processing.

**Note:** Must be registered, logged in, and authorized to submit a loan application.

**Instructions:**
1. Make sure you are authorized — click **Authorize** at the top and paste your token from `POST /auth/demo`
2. Click **Try it out** then **Execute** — the fields are pre-populated with demo values
3. Copy the `transaction_id` from the response — this is your **loan ID**
4. Use that `transaction_id` in `GET /loans/{loan_id}` to check your loan status

Credit Score categories:
 - 500-649 -> review (for human intervention)
 - 650+ -> approved (Max FICO is 850)
 - anything less than 500 is rejected (Min FICO is 300)

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def submit_loan_application(request: LoanApplicationRequest, req: Request):
    # Same identity-extraction pattern as update_loan_status — API Gateway's
    # Lambda Authorizer already confirmed this token is VALID before we got
    # here; this step is purely about knowing WHO it belongs to, so we can
    # record it in the audit vault.
    auth_header = req.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "")
    payload = verify_jwt(token)
    actor = payload["account_id"] if payload else "unknown"

    return await loans_repository.submit_loan_application(request, actor)


@router.get(
        "/{loan_id}",
        response_model=LoanApplicationResponse,
        status_code=201,
        summary="Get Loan Status",
        description="""
Retrieve a loan application by its ID.

**Note:** Must be registered, logged in, and authorized to check your loan status.

**Instructions:**
1. Copy the `transaction_id` (returned from the `POST /loans/` response) into the `loan_id` field
2. Click **Try it out**
3. Paste the `transaction_id` into the `loan_id` field
4. Click **Execute**

The `loan_id` is the same value as `transaction_id` — it's your unique loan reference number.

**Status lifecycle:** `pending` → `funded` (allow a few seconds for async processing)

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def get_loan(
    loan_id: str = Path(
        ...,
        description="The transaction_id returned from POST /loans/",
        examples=["YOUR_TRANSACTION_ID"]
    )
): 
    return await loans_repository.get_loan(loan_id)


@router.get(
        "/account/{account_id}",
        summary="Get All Loans by Account",
        description="""
Retrieve all loan transactions for a specific account.

**Note:** Must be registered, logged in, and authorized to view your account transactions.

**Instructions:**
1. Click **Try it out**
2. Enter `acc_demo_001` in the `account_id` (returned from the `POST /auth/demo` response) field — this is the demo account
3. Click **Execute**
4. If using your own customer account, enter your `account_id` from the login response and click **Execute**

Returns all transactions associated with that account ordered by timestamp.

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def get_loans_by_account(account_id: str):
    return await loans_repository.get_loans_by_account(account_id)


@router.get(
        "/customer/{customer_id}",
        summary="Get All Loans by Customer",
        description="""
Retrieve all loan transactions across all accounts for a specific customer.

**Note:** Must be registered, logged in, and authorized to check all of your customer loans.

**Instructions:**
1. Click **Try it out**
2. Enter `cust_demo_001` in the `customer_id` (returned from the `POST /auth/demo` response) field — this is the demo customer
3. Click **Execute**
4. If using your own customer account, enter your `customer_id` from the login response and click **Execute**

A customer can have multiple accounts — this endpoint returns transactions across all of them via GSI 2.

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def get_loans_by_customer(customer_id: str):
    return await loans_repository.get_loans_by_customer(customer_id)


@router.patch(
        "/{loan_id}/status",
        response_model=LoanApplicationResponse,
        summary="Update Loan Status",
        description="""
Update the status of a loan application, following the loan lifecycle rules.

**Valid transitions:**
- `pending` -> `review`, `approved`, `rejected` use-case is only if the worker is unable to process the loan automatically — a human can intervene and move it forward manually
- `review` -> `approved`, `rejected` these are updates that can only happen if the score is in the gray zone (500-649). If your loan is in review refer to these updates to move it forward.
- `approved` -> `funded`, `repaid`, `defaulted` these are updates that can happen once a human (you) has approved the loan — unless the credit score is high enough to auto-approve
- `funded` -> `repaid`, `defaulted` these are updates that can happen once a human (you) has approved and funded the loan
- `rejected`, `repaid`, `defaulted` are FINAL — cannot be changed once reached

**Note:** Must be registered, logged in, and authorized to update a loan status.

**Instructions:**
1. Submit a loan via `POST /loans/` and copy the `transaction_id` (returned in the response) — this is your **loan ID**
2. Click **Try it out**
3. Paste the `transaction_id` into the `loan_id` field
4. Set a status that's a valid next step for the loan's current state (Refer to the Valid transitions above)
5. Click **Execute**

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def update_loan_status(loan_id: str, status_update: LoanStatusUpdate, req: Request):
    auth_header = req.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "")
    payload = verify_jwt(token)
    actor = payload["account_id"] if payload else "unknown"
    return await loans_repository.update_loan_status(loan_id, status_update.status.value, actor)
