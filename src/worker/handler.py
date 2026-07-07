import json
import time
import uuid
from datetime import datetime, timezone
from boto3.dynamodb.conditions import Attr
from src.api.core.database import get_transactions_table
from src.api.core.security import encrypt_pii
from src.api.core.cache import cache_delete
from src.api.core.audit import write_audit_event


# Outside handler — L1 cached in execution context
_table = None


def get_table():
    global _table
    if _table is None:
        _table = get_transactions_table()
    return _table


def handler(event, context):
    table = get_table()
    
    # SQS sends messages in batches — process each one
    # This is your batch operations talking point
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            process_transaction(table, body)
        except Exception as e:
            print(f"Failed to process record: {record['messageId']} — {str(e)}")
            # Raising here tells SQS this message failed
            # SQS will retry it based on my queue's retry policy
            raise


def process_transaction(table, transaction_data: dict):
    transaction_id = transaction_data["transaction_id"]
    transaction_type = transaction_data["type"]

    if transaction_type == "transfer":
        process_transfer(table, transaction_data)
    else:
        process_single(table, transaction_data)


def process_single(table, transaction_data: dict):
    transaction_id = transaction_data["transaction_id"]
    account_id = transaction_data["account_id"]
    timestamp = transaction_data["timestamp"]
    credit_score = transaction_data["credit_score"]

    loan_status = determine_loan_status(credit_score)

    write_audit_event(
    transaction_id=transaction_id,
    event_type="credit_score_pulled",
    actor="system:worker",
    details={"credit_score": credit_score, "resulting_status": loan_status}
    )

    # Conditional write — attribute_not_exists prevents duplicate processing
    # Second layer of duplicate prevention after SQS FIFO deduplication
    # Same pattern as JWT single-use tokens in my Content Moderation API
    try:
        table.put_item(
            Item={
                "account_id": account_id,
                "timestamp_transaction_id": f"{timestamp}#{transaction_id}",
                "transaction_id": transaction_id,
                "customer_id": transaction_data["customer_id"],
                "timestamp": timestamp,
                "amount": str(transaction_data["amount"]),
                "credit_score": credit_score,  # stored for audit trail / underwriting history
                "status": loan_status,
                "type": transaction_data["type"],
                "description": transaction_data.get("description", ""),
            },
            ConditionExpression=Attr("transaction_id").not_exists()
        )

        # Invalidate cache for this account — stale data must not be served
        cache_delete(f"account:{account_id}:transactions")
        cache_delete(f"customer:{transaction_data['customer_id']}:transactions")

    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Transaction already exists — duplicate caught at DB level
        # Log it but don't raise — this is expected behavior, not an error
        print(f"Duplicate transaction caught at DB level: {transaction_id}")


def process_transfer(table, transaction_data: dict):
    # scoring doesn't apply here. If you tried to run determine_loan_status()
    # on a transfer, there'd be no credit_score in the message anyway since
    transaction_id = transaction_data["transaction_id"]
    source_account = transaction_data["account_id"]
    target_account = transaction_data["target_account_id"]
    timestamp = transaction_data["timestamp"]
    amount = str(transaction_data["amount"])

    # DynamoDB Transaction — both writes succeed or both fail
    # Atomicity is required — can't debit one account without crediting the other
    # A partial write in a lending system is a compliance issue not just a bug
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            "account_id": source_account,
                            "timestamp_transaction_id": f"{timestamp}#{transaction_id}-debit",
                            "transaction_id": f"{transaction_id}-debit",
                            "customer_id": transaction_data["customer_id"],
                            "timestamp": timestamp,
                            "amount": amount,
                            "status": "funded",
                            "type": "transfer_debit",
                            "description": f"Transfer to {target_account}",
                        },
                        "ConditionExpression": "attribute_not_exists(transaction_id)"
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {
                            "account_id": target_account,
                            "timestamp_transaction_id": f"{timestamp}#{transaction_id}-credit",
                            "transaction_id": f"{transaction_id}-credit",
                            "customer_id": transaction_data.get("target_customer_id", ""),
                            "timestamp": timestamp,
                            "amount": amount,
                            "status": "completed",
                            "type": "transfer_credit",
                            "description": f"Transfer from {source_account}",
                        },
                        "ConditionExpression": "attribute_not_exists(transaction_id)"
                    }
                }
            ]
        )

        # Invalidate cache for both accounts
        cache_delete(f"account:{source_account}:transactions")
        cache_delete(f"account:{target_account}:transactions")

    except Exception as e:
        print(f"Transfer failed — transaction rolled back: {transaction_id} — {str(e)}")
        raise

def determine_loan_status(credit_score: int) -> str:
    # Credit-based loan tier routing.
    # >=650: auto-approved (FICO "Good" tier and above)
    # >=500: routed to human review (gray zone, needs judgment)
    # <500:  auto-rejected (deep subprime)
    
    if credit_score >= 650:
        return "approved"
    elif credit_score >= 500:
        return "review"
    else:
        return "rejected"
