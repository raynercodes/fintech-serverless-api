import os
import uuid
import json
import boto3
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Key
from fastapi import HTTPException
from src.api.core.database import get_transactions_table
from src.api.core.cache import cache_get, cache_set, cache_delete
from src.api.core.audit import write_audit_event
from src.api.core.idempotency import compute_content_hash, claim_duplicate_check, get_duplicate_transaction_id
from src.api.models.loan import LoanApplicationRequest, LoanApplicationResponse, LoanStatus

# Outside handler — L1 cached in execution context
_table = None
_sqs = None


def get_table():
    global _table
    if _table is None:
        _table = get_transactions_table()
    return _table


def get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name="us-east-1")
    return _sqs


# Loan lifecycle state machine — the ONLY source of truth for what
# transitions are legal. Each key is a CURRENT status, each value is
# the SET of statuses it's allowed to move to. An empty set means
# that status is FINAL — nothing can ever leave it again.
VALID_TRANSITIONS = {
    "pending":   {"review", "approved", "rejected"},
    "review":    {"approved", "rejected"},
    "approved":  {"funded", "repaid", "defaulted"},
    "rejected":  set(),
    "funded":    {"repaid", "defaulted"},
    "repaid":    set(),
    "defaulted": set(),
}


# ============================================================
# 1. POST / — submit a new loan application
# ============================================================
async def submit_loan_application(request: LoanApplicationRequest, actor: str) -> LoanApplicationResponse:
    # NEW — fingerprint this request's actual content BEFORE doing
    # anything else. This is the automatic duplicate-submission guard —
    # catches a double-click or a dropped-connection retry regardless
    # of how the SQS 5-minute dedup window would've handled it.
    content_hash = compute_content_hash(
        request.account_id, request.customer_id, float(request.amount),
        request.type.value, request.description, request.credit_score
    )

    # Has this exact content been submitted in the last 90 seconds?
    # If so, don't create anything new — hand back the ORIGINAL result.
    existing_transaction_id = get_duplicate_transaction_id(content_hash)
    if existing_transaction_id:
        return await get_loan(existing_transaction_id)

    transaction_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Claim this content hash now that we have a transaction_id to
    # associate it with. If this fails, someone raced us and claimed
    # the same content a moment ago — treat it the same as above.
    claimed = claim_duplicate_check(content_hash, transaction_id)
    if not claimed:
        existing_transaction_id = get_duplicate_transaction_id(content_hash)
        return await get_loan(existing_transaction_id)

    # Build the message that goes to SQS
    # This is the contract the worker Lambda expects
    message = {
        "transaction_id": transaction_id,
        "account_id": request.account_id,
        "customer_id": request.customer_id,
        "amount": float(request.amount),
        "type": request.type.value,
        "credit_score": request.credit_score,
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
            # (the idempotency check above is the SECOND, stronger layer,
            # since this one only catches redelivery within 5 minutes)
            MessageDeduplicationId=transaction_id
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail="unavailable to proccess request"
        )

    # NEW — written only AFTER the SQS send succeeds. If send_message()
    # raised above, we never reach this line — no false audit record
    # claiming a submission happened when it actually failed.
    write_audit_event(
        transaction_id=transaction_id,
        event_type="loan_submitted",
        actor=actor,
        details={
            "account_id": request.account_id,
            "customer_id": request.customer_id,
            "amount": float(request.amount),
            "type": request.type.value
        }
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


# ============================================================
# 2. GET /{loan_id} — fetch a single loan by transaction_id
# ============================================================
async def get_loan(loan_id: str) -> LoanApplicationResponse:
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


# ============================================================
# 3. GET /account/{account_id} — all loans for one account
# ============================================================
async def get_loans_by_account(account_id: str) -> list:
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


# ============================================================
# 4. GET /customer/{customer_id} — all loans across all of a
#    customer's accounts
# ============================================================
async def get_loans_by_customer(customer_id: str) -> list:
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


# ============================================================
# 5. PATCH /{loan_id}/status — move a loan through its lifecycle
# ============================================================
async def update_loan_status(loan_id: str, requested_status: str, actor: str) -> LoanApplicationResponse:
    table = get_table()

    # First fetch the item to get account_id, sort key, AND current status
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
    current_status = item["status"]

    # State machine check — is the REQUESTED status actually a legal
    # move from the loan's CURRENT status? This runs in Python, before
    # DynamoDB is ever touched, so an illegal transition never costs
    # a write attempt at all.
    allowed_next_states = VALID_TRANSITIONS.get(current_status, set())
    if requested_status not in allowed_next_states:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move loan from '{current_status}' to '{requested_status}'. "
                   f"Valid next states from '{current_status}': {sorted(allowed_next_states) or 'none (final state)'}"
        )

    # Conditional update — status must STILL match what we just read.
    # Prevents race conditions when two requests hit the same item at
    # the same time — whichever writes first wins, the second's
    # condition fails because the status it expected is already gone.
    try:
        result = table.update_item(
            Key={
                "account_id": account_id,
                "timestamp_transaction_id": sort_key
            },
            UpdateExpression="SET #s = :new_status",
            ConditionExpression="#s = :current_status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new_status": requested_status,
                ":current_status": current_status
            },
            ReturnValues="ALL_NEW"
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # This specifically means a genuine RACE — someone else changed
        # this loan between our query above and this update. Business
        # rule violations are already caught above, before this point.
        raise HTTPException(
            status_code=409,
            detail="Loan status changed by another request — please refresh and try again"
        )

    # Audit trail — "who approved a loan, and when" requirement.
    # Written AFTER the DynamoDB update succeeds — if the update fails,
    # we never log an event claiming something happened that didn't.
    write_audit_event(
        transaction_id=loan_id,
        event_type="status_changed",
        actor=actor,
        details={"from_status": current_status, "to_status": requested_status}
    )

    # Separate, dedicated event for "when funds were disbursed" — its
    # own named requirement in the compliance description. Lets an
    # auditor query the vault for "loan_disbursed" events directly,
    # instead of filtering status_changed events by to_status=="funded".
    if requested_status == "funded":
        write_audit_event(
            transaction_id=loan_id,
            event_type="loan_disbursed",
            actor=actor,
            details={"amount": float(item["amount"])}
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