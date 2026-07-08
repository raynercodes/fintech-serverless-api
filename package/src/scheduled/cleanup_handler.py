import os
import time
import boto3
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Attr
from src.api.core.audit import write_audit_event

_table = None

# 24 hours — loans normally process within seconds via the SQS worker.
# Still being "pending" after this long means something genuinely
# broke (worker crash, stuck message, silent bug) — not normal latency.
STALE_THRESHOLD_SECONDS = 24 * 60 * 60


def get_table():
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _table = dynamodb.Table(os.environ["DYNAMODB_TABLE_NAME"])
    return _table


def handler(event, context):
    """
    Runs daily via EventBridge. Scans for loans stuck in "pending"
    longer than STALE_THRESHOLD_SECONDS and flags them — does NOT
    delete or change their status. Financial transaction records are
    never deleted in this system, full stop; this job only draws
    attention to ones that need a human to look at them.

    NOTE ON SCAN: this uses a full table Scan rather than a Query,
    since there's no GSI on `status` right now. That's a deliberate,
    documented tradeoff at current portfolio scale — a Scan reads
    every item in the table, which would become genuinely expensive
    and slow at high transaction volume. Adding a status-based GSI
    would be the production fix if this table ever grew large; noted
    here rather than pretending Scan is free at any scale.
    """
    table = get_table()
    now = int(time.time())
    flagged_count = 0

    # Scan for pending items not already flagged — avoids re-flagging
    # (and re-logging/re-auditing) the same stuck item every single day
    response = table.scan(
        FilterExpression=Attr("status").eq("pending") & Attr("stale_flagged").not_exists()
    )

    items = response.get("Items", [])

    for item in items:
        # timestamp is stored as an ISO string — convert to epoch seconds
        # to do real math against "now"
        submitted_time = datetime.fromisoformat(item["timestamp"]).timestamp()
        age_seconds = now - submitted_time

        if age_seconds > STALE_THRESHOLD_SECONDS:
            hours_pending = round(age_seconds / 3600, 1)

            # Log loudly — this lands in CloudWatch automatically,
            # visible in the Lambda's log group with clear, greppable text
            print(f"STALE TRANSACTION DETECTED: {item['transaction_id']} "
                  f"has been pending for {hours_pending} hours (account: {item['account_id']})")

            # Flag the item — metadata only, status untouched
            table.update_item(
                Key={
                    "account_id": item["account_id"],
                    "timestamp_transaction_id": item["timestamp_transaction_id"]
                },
                UpdateExpression="SET stale_flagged = :true, stale_flagged_at = :now",
                ExpressionAttributeValues={
                    ":true": True,
                    ":now": now
                }
            )

            # Same compliance vault every other lifecycle event uses —
            # a human reviewing this loan later can see EXACTLY when
            # and why it got flagged, alongside everything else that
            # happened to it
            write_audit_event(
                transaction_id=item["transaction_id"],
                event_type="stale_pending_flagged",
                actor="system:cleanup",
                details={"hours_pending": hours_pending, "account_id": item["account_id"]}
            )

            flagged_count += 1

    print(f"Cleanup run complete — flagged {flagged_count} stale transaction(s)")
    return {"flagged_count": flagged_count}
