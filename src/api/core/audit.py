import json
import os
import boto3
from datetime import datetime, timezone

# Outside handler — L1 cached in execution context, same pattern as
# every other boto3 client in this project
_s3 = None


def get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name="us-east-1")
    return _s3


def write_audit_event(transaction_id: str, event_type: str, actor: str, details: dict = None) -> None:
    """
    Writes ONE permanent, tamper-proof record to the Compliance Vault.

    Each event becomes its own small JSON object, keyed so every event
    for a given loan naturally sorts together in S3:
        {transaction_id}/{timestamp}-{event_type}.json

    event_type examples: "credit_score_pulled", "status_changed", "loan_disbursed"
    actor: who/what triggered this — a real account_id from a JWT,
           or "system:worker" for fully automated decisions
    details: event-specific context, e.g. {"from_status": "review", "to_status": "approved"}
    """
    s3 = get_s3()
    timestamp = datetime.now(timezone.utc).isoformat()

    record = {
        "transaction_id": transaction_id,
        "event_type": event_type,
        "actor": actor,
        "timestamp": timestamp,
        "details": details or {}
    }

    key = f"{transaction_id}/{timestamp}-{event_type}.json"

    # NOTE: no special parameters needed to "lock" this object — the
    # bucket's DefaultRetention rule applies automatically to every
    # object written here. We don't set retention per-call.
    s3.put_object(
        Bucket=os.environ["AUDIT_LOG_BUCKET_NAME"],
        Key=key,
        Body=json.dumps(record),
        ContentType="application/json"
    )