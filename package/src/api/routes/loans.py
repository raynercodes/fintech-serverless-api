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

router = APIRouter()
bearer_scheme = HTTPBearer()

# Outside handler — L1 cached in execution context
_sqs = None
_table = None


def get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name="us-east-1")
    return _sqs


def get_table():
    global _table
    if _table is None:
        _table = get_transactions_table()
    return _table


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

Processing is async — status starts as `pending` and updates to `funded` within a few seconds once the worker processes it via SQS FIFO.

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def submit_loan_application(request: LoanApplicationRequest, req: Request):
    transaction_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Build the message that goes to SQS
    # This is the contract the worker Lambda expects
    message = {
        "transaction_id": transaction_id,
        "account_id": request.account_id,
        "customer_id": request.customer_id,
        "amount": float(request.amount),
        "type": request.type.value,
        "timestamp": timestamp,
        "description": request.description or ""
    }

    if request.type.value == "transfer":
        target_account = getattr(request, "target_account_id", None)
        if not target_account:
            raise HTTPException(
                status_code=400,
                detail="target_account_id is required for transfers"
            )
        message["target_account_id"] = target_account

    # Push to SQS FIFO — async processing
    # API never blocks waiting for DynamoDB write
    # Same pattern as Celery in my Content Moderation API
    try:
        sqs = get_sqs()
        sqs.send_message(
            QueueUrl=os.environ["SQS_QUEUE_URL"],
            MessageBody=json.dumps(message),
            # MessageGroupId ensures FIFO ordering per account
            MessageGroupId=request.account_id,
            # MessageDeduplicationId prevents duplicate messages
            # First layer of duplicate prevention — SQS FIFO level
            MessageDeduplicationId=transaction_id
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail="unavailable to proccess request"
        )

    # Return pending status instantly — worker handles the actual write
    # GSI eventual consistency handled by returning full data here
    # Client has the transaction_id to poll for status updates
    return LoanApplicationResponse(
        transaction_id=transaction_id,
        account_id=request.account_id,
        customer_id=request.customer_id,
        amount=request.amount,
        type=request.type,
        status=LoanStatus.pending,
        timestamp=timestamp,
        description=request.description
    )


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
    # L1/L2 cache check first
    cache_key = f"loan:{loan_id}"
    cached = cache_get(cache_key)
    if cached:
        return LoanApplicationResponse(**cached)

    # Cache miss — query GSI 1 (transaction-id-index)
    # This is the GET /loans/{loan_id} access pattern
    table = get_table()
    try:
        result = table.query(
            IndexName="transaction-id-index",
            KeyConditionExpression=Key("transaction_id").eq(loan_id)
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    items = result.get("Items", [])
    if not items:
        raise HTTPException(status_code=404, detail="Loan not found")

    item = items[0]

    response = LoanApplicationResponse(
        transaction_id=item["transaction_id"],
        account_id=item["account_id"],
        customer_id=item["customer_id"],
        amount=float(item["amount"]),
        type=item["type"],
        status=item["status"],
        timestamp=item["timestamp"],
        description=item.get("description")
    )

    # Store in cache — 5 minute TTL
    cache_set(cache_key, response.model_dump(), ttl_seconds=300)
    return response


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
    # L1/L2 cache check first
    cache_key = f"account:{account_id}:transactions"
    cached = cache_get(cache_key)
    if cached:
        return cached

    # Cache miss — query main table directly by partition key
    # No GSI needed — account_id IS the partition key
    table = get_table()
    try:
        result = table.query(
            KeyConditionExpression=Key("account_id").eq(account_id)
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    items = result.get("Items", [])
    response = [
        {
            "transaction_id": item["transaction_id"],
            "account_id": item["account_id"],
            "amount": float(item["amount"]),
            "type": item["type"],
            "status": item["status"],
            "timestamp": item["timestamp"],
        }
        for item in items
    ]

    # Cache the result — 30 second TTL
    # Short TTL because new transactions invalidate this list
    cache_set(cache_key, response, ttl_seconds=30)
    return response


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
    # L1/L2 cache check first
    cache_key = f"customer:{customer_id}:transactions"
    cached = cache_get(cache_key)
    if cached:
        return cached

    # Cache miss — query GSI 2 (customer-id-index)
    # This is the GET /loans/customer/{customer_id} access pattern
    table = get_table()
    try:
        result = table.query(
            IndexName="customer-id-index",
            KeyConditionExpression=Key("customer_id").eq(customer_id)
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    items = result.get("Items", [])
    response = [
        {
            "transaction_id": item["transaction_id"],
            "account_id": item["account_id"],
            "customer_id": item["customer_id"],
            "amount": float(item["amount"]),
            "type": item["type"],
            "status": item["status"],
            "timestamp": item["timestamp"],
        }
        for item in items
    ]

    cache_set(cache_key, response, ttl_seconds=30)
    return response


@router.patch(
        "/{loan_id}/status",
        response_model=LoanApplicationResponse,
        summary="Update Loan Status",
        description="""
Update the status of a pending loan application.

**Note:** Only `pending` loans can be updated — this prevents race conditions where two requests try to update the same loan simultaneously.
      Must be registered, logged in, and authorized to update a loan status.

**Instructions:**
1. Submit a loan via `POST /loans/` and copy the `transaction_id` (returned in the response) — this is your **loan ID**
2. Click **Try it out**
3. Paste the `transaction_id` into the `loan_id` field
4. Set status to `approved`, `funded`, `repaid`, or `defaulted`
5. Click **Execute**

**Note:** You can always refer back to the Steps at the top of the page when needed.
    """,
        dependencies=[Depends(bearer_scheme)]
)
async def update_loan_status(loan_id: str, status_update: LoanStatusUpdate):
    table = get_table()

    # First fetch the item to get account_id and sort key
    # GSI 1 doesn't support updates directly — need main table keys
    try:
        result = table.query(
            IndexName="transaction-id-index",
            KeyConditionExpression=Key("transaction_id").eq(loan_id)
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    items = result.get("Items", [])
    if not items:
        raise HTTPException(status_code=404, detail="Loan not found")

    item = items[0]
    account_id = item["account_id"]
    sort_key = item["timestamp_transaction_id"]

    # Conditional update — status must be pending to allow transition
    # Prevents race conditions when two Lambda instances hit the same item
    try:
        result = table.update_item(
            Key={
                "account_id": account_id,
                "timestamp_transaction_id": sort_key
            },
            UpdateExpression="SET #s = :new_status",
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new_status": status_update.status.value,
                ":pending": "pending"
            },
            ReturnValues="ALL_NEW"
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise HTTPException(
            status_code=409,
            detail="Loan is no longer in pending status"
        )

    # Invalidate cache — status changed, cached data is stale
    cache_delete(f"loan:{loan_id}")
    cache_delete(f"account:{account_id}:transactions")

    updated = result["Attributes"]
    return LoanApplicationResponse(
        transaction_id=updated["transaction_id"],
        account_id=updated["account_id"],
        customer_id=updated["customer_id"],
        amount=float(updated["amount"]),
        type=updated["type"],
        status=updated["status"],
        timestamp=updated["timestamp"],
        description=updated.get("description")
    )
