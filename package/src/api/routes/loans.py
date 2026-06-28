import json
import uuid
import os
import boto3
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
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


@router.post("/", response_model=LoanApplicationResponse, status_code=201)
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


@router.get("/{loan_id}", response_model=LoanApplicationResponse)
async def get_loan(loan_id: str):
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


@router.get("/account/{account_id}")
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


@router.get("/customer/{customer_id}")
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


@router.patch("/{loan_id}/status", response_model=LoanApplicationResponse)
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
