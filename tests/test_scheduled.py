import pytest
import boto3
import json
import os
import time
from datetime import datetime, timezone, timedelta
from moto import mock_aws
from unittest.mock import MagicMock

os.environ["DYNAMODB_TABLE_NAME"] = "fintech-transactions-test"
os.environ["CACHE_TABLE_NAME"] = "fintech-cache-test"
os.environ["USERS_TABLE_NAME"] = "fintech-users-test"
os.environ["AUDIT_LOG_BUCKET_NAME"] = "fintech-audit-log-test"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

from src.scheduled.cleanup_handler import handler as cleanup_handler
from src.scheduled.backup_handler import handler as backup_handler, _delete_old_backups, BACKUP_RETENTION_DAYS


@pytest.fixture
def dynamodb_tables(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        transactions_table = dynamodb.create_table(
            TableName="fintech-transactions-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "account_id", "AttributeType": "S"},
                {"AttributeName": "timestamp_transaction_id", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "account_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp_transaction_id", "KeyType": "RANGE"},
            ],
        )

        users_table = dynamodb.create_table(
            TableName="fintech-users-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}],
        )

        yield transactions_table, users_table


@pytest.fixture
def audit_log_bucket(aws_credentials):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=os.environ["AUDIT_LOG_BUCKET_NAME"])
        yield s3


def iso_hours_ago(hours: float) -> str:
    """Builds an ISO timestamp a given number of hours in the past —
    lets us seed items that are artificially 'old' without waiting."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def seed_transaction(table, transaction_id, account_id, status, timestamp_iso, extra=None):
    item = {
        "account_id": account_id,
        "timestamp_transaction_id": f"{timestamp_iso}#{transaction_id}",
        "transaction_id": transaction_id,
        "customer_id": "cust_001",
        "timestamp": timestamp_iso,
        "amount": "5000.00",
        "credit_score": 650,
        "status": status,
        "type": "deposit",
        "description": "seeded test transaction",
    }
    if extra:
        item.update(extra)
    table.put_item(Item=item)


def get_audit_events(s3_client, transaction_id: str) -> list:
    result = s3_client.list_objects_v2(
        Bucket=os.environ["AUDIT_LOG_BUCKET_NAME"],
        Prefix=f"{transaction_id}/"
    )
    events = []
    for obj in result.get("Contents", []):
        body = s3_client.get_object(Bucket=os.environ["AUDIT_LOG_BUCKET_NAME"], Key=obj["Key"])
        events.append(json.loads(body["Body"].read()))
    return events


# ============================================================
# CLEANUP HANDLER TESTS
# ============================================================

def test_cleanup_flags_stale_pending_loan(dynamodb_tables, audit_log_bucket):
    """The core case — a loan stuck in pending for 25 hours should get flagged."""
    transactions_table, _ = dynamodb_tables
    stale_timestamp = iso_hours_ago(25)
    seed_transaction(transactions_table, "txn-stale-001", "acc_001", "pending", stale_timestamp)

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 1

    item = transactions_table.get_item(
        Key={"account_id": "acc_001", "timestamp_transaction_id": f"{stale_timestamp}#txn-stale-001"}
    )["Item"]
    assert item["stale_flagged"] is True
    assert "stale_flagged_at" in item

    events = get_audit_events(audit_log_bucket, "txn-stale-001")
    assert len(events) == 1
    assert events[0]["event_type"] == "stale_pending_flagged"
    assert events[0]["actor"] == "system:cleanup"


def test_cleanup_does_not_flag_fresh_pending_loan(dynamodb_tables, audit_log_bucket):
    """A loan pending for only a few minutes is completely normal — no flag."""
    transactions_table, _ = dynamodb_tables
    fresh_timestamp = iso_hours_ago(0.1)
    seed_transaction(transactions_table, "txn-fresh-001", "acc_001", "pending", fresh_timestamp)

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 0

    item = transactions_table.get_item(
        Key={"account_id": "acc_001", "timestamp_transaction_id": f"{fresh_timestamp}#txn-fresh-001"}
    )["Item"]
    assert "stale_flagged" not in item


def test_cleanup_ignores_non_pending_status(dynamodb_tables, audit_log_bucket):
    """An old APPROVED loan isn't stuck — it already moved through the
    lifecycle correctly. Only 'pending' represents something that
    should have resolved almost instantly and didn't."""
    transactions_table, _ = dynamodb_tables
    old_timestamp = iso_hours_ago(48)
    seed_transaction(transactions_table, "txn-approved-old", "acc_001", "approved", old_timestamp)

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 0


def test_cleanup_does_not_reflag_already_flagged_loan(dynamodb_tables, audit_log_bucket):
    """Prevents the job from re-flagging (and re-auditing) the same
    stuck item every single day it runs — the FilterExpression excludes
    anything with stale_flagged already set."""
    transactions_table, _ = dynamodb_tables
    stale_timestamp = iso_hours_ago(30)
    seed_transaction(
        transactions_table, "txn-already-flagged", "acc_001", "pending", stale_timestamp,
        extra={"stale_flagged": True, "stale_flagged_at": int(time.time()) - 3600}
    )

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 0

    events = get_audit_events(audit_log_bucket, "txn-already-flagged")
    assert len(events) == 0


def test_cleanup_boundary_just_over_threshold(dynamodb_tables, audit_log_bucket):
    """24.1 hours — just past the 24-hour line, should flag."""
    transactions_table, _ = dynamodb_tables
    just_over = iso_hours_ago(24.1)
    seed_transaction(transactions_table, "txn-boundary-over", "acc_001", "pending", just_over)

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 1


def test_cleanup_boundary_just_under_threshold(dynamodb_tables, audit_log_bucket):
    """23.9 hours — just under the line, should NOT flag. This is the
    off-by-one boundary check, same principle as the credit score tests."""
    transactions_table, _ = dynamodb_tables
    just_under = iso_hours_ago(23.9)
    seed_transaction(transactions_table, "txn-boundary-under", "acc_001", "pending", just_under)

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 0


def test_cleanup_flags_multiple_stale_loans_correctly(dynamodb_tables, audit_log_bucket):
    """Confirms flagged_count actually reflects the real number found,
    not just 0 or 1 — and that a fresh item mixed in doesn't get swept up."""
    transactions_table, _ = dynamodb_tables
    seed_transaction(transactions_table, "txn-multi-1", "acc_001", "pending", iso_hours_ago(30))
    seed_transaction(transactions_table, "txn-multi-2", "acc_002", "pending", iso_hours_ago(48))
    seed_transaction(transactions_table, "txn-multi-3", "acc_003", "pending", iso_hours_ago(1))

    result = cleanup_handler({}, None)
    assert result["flagged_count"] == 2


# ============================================================
# BACKUP HANDLER TESTS
# ============================================================

def test_backup_creates_backups_for_both_tables(dynamodb_tables):
    result = backup_handler({}, None)

    assert len(result["created_backups"]) == 2
    assert any("fintech-transactions-test" in name for name in result["created_backups"])
    assert any("fintech-users-test" in name for name in result["created_backups"])


def test_backup_naming_includes_backup_marker(dynamodb_tables):
    result = backup_handler({}, None)
    for name in result["created_backups"]:
        assert "-backup-" in name


def test_delete_old_backups_prunes_only_expired():
    """
    ISOLATED unit test — deliberately NOT using moto here. Moto always
    creates backups stamped with the real current time, so there's no
    way to make a moto-created backup actually be 40 days old. Instead,
    we hand a mocked boto3 client a FAKE list_backups response with
    controlled BackupCreationDateTime values — one clearly expired,
    one clearly recent — and confirm only the expired one gets deleted.
    """
    mock_client = MagicMock()
    now = datetime.now(timezone.utc)
    old_backup_time = now - timedelta(days=BACKUP_RETENTION_DAYS + 5)
    recent_backup_time = now - timedelta(days=5)

    mock_client.list_backups.return_value = {
        "BackupSummaries": [
            {
                "BackupName": "old-backup",
                "BackupArn": "arn:aws:dynamodb:us-east-1:123456789:table/test/backup/old",
                "BackupCreationDateTime": old_backup_time
            },
            {
                "BackupName": "recent-backup",
                "BackupArn": "arn:aws:dynamodb:us-east-1:123456789:table/test/backup/recent",
                "BackupCreationDateTime": recent_backup_time
            }
        ]
    }

    _delete_old_backups(mock_client, "fintech-transactions-test")

    mock_client.delete_backup.assert_called_once_with(
        BackupArn="arn:aws:dynamodb:us-east-1:123456789:table/test/backup/old"
    )