import os
import boto3
from datetime import datetime, timezone, timedelta

_dynamodb_client = None

# Keep 35 days of backups on a rolling basis — long enough to recover
# from a real problem discovered late, short enough not to accumulate
# storage cost indefinitely. Real fintech compliance windows are
# measured in years (that's what the S3 Object Lock audit vault is
# for) — these DynamoDB backups are a disaster-recovery safety net,
# not the compliance record itself.
BACKUP_RETENTION_DAYS = 35


def get_client():
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb", region_name="us-east-1")
    return _dynamodb_client


def handler(event, context):
    """
    Runs daily via EventBridge. Creates an on-demand backup of the
    tables that actually hold real data worth protecting — NOT the
    cache table, since that's disposable/ephemeral by design (TTL'd
    cache entries and brute-force lockout records, nothing worth
    preserving in a backup).

    Also prunes backups older than BACKUP_RETENTION_DAYS, so this
    doesn't silently accumulate storage cost forever — same cost
    discipline as everything else in this project.
    """
    client = get_client()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    tables_to_backup = [
        os.environ["DYNAMODB_TABLE_NAME"],   # transactions
        os.environ["USERS_TABLE_NAME"],      # users
    ]

    created_backups = []

    for table_name in tables_to_backup:
        backup_name = f"{table_name}-backup-{timestamp}"
        response = client.create_backup(
            TableName=table_name,
            BackupName=backup_name
        )
        created_backups.append(backup_name)
        print(f"Created backup: {backup_name}")

        # Prune old backups for THIS table — only delete ones past
        # the retention window, never touch recent ones
        _delete_old_backups(client, table_name)

    return {"created_backups": created_backups}


def _delete_old_backups(client, table_name: str):
    # compare backup creation time against this cutoff
    # anything older than this is considered expired and will be deleted
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)

    response = client.list_backups(TableName=table_name)
    deleted_count = 0

    for backup in response.get("BackupSummaries", []):
        backup_creation_time = backup["BackupCreationDateTime"]
        if backup_creation_time < cutoff:
            client.delete_backup(BackupArn=backup["BackupArn"])
            deleted_count += 1
            print(f"Deleted expired backup: {backup['BackupName']} (created {backup_creation_time})")

    if deleted_count:
        print(f"Pruned {deleted_count} expired backup(s) for {table_name}")
