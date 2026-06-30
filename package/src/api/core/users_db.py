import boto3
import os
from datetime import datetime, timezone

# Outside handler — L1 cached in Lambda execution context
# Same pattern as database.py for the transactions table
_dynamodb = None
_users_table = None


def get_users_table():
    global _dynamodb, _users_table
    if _users_table is None:
        _dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _users_table = _dynamodb.Table(os.environ["USERS_TABLE_NAME"])
    return _users_table


def get_user_by_email(email: str) -> dict:
    # Fetch a user record by email — the partition key.
    # Returns the full item or None if not found.
    #Used during login to verify credentials.
    try:
        table = get_users_table()
        result = table.get_item(Key={"email": email.lower()})
        return result.get("Item")
    except Exception as e:
        print(f"Error fetching user: {str(e)}")
        return None


def create_user(
    email: str,
    password_hash: str,
    account_id: str,
    customer_id: str
) -> dict:
    # Create a new user record.
    # Uses conditional write — attribute_not_exists(email) prevents
    # duplicate registrations at the database level.
    # Same pattern as duplicate transaction prevention in the worker.
    try:
        table = get_users_table()
        now = datetime.now(timezone.utc).isoformat()

        item = {
            # email stored lowercase — prevents acc@email.com and ACC@email.com
            # being treated as different accounts
            "email": email.lower(),
            "password_hash": password_hash,
            "account_id": account_id,
            "customer_id": customer_id,
            "created_at": now,
            "updated_at": now,
            "is_active": True
        }

        table.put_item(
            Item=item,
            # Conditional write — email must not already exist
            # Prevents duplicate accounts without a separate lookup first
            ConditionExpression="attribute_not_exists(email)"
        )

        return item

    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Email already registered — return None to signal conflict
        # Caller decides whether to raise 409 or a generic message
        # Generic message preferred — don't confirm whether email exists
        # That information helps attackers enumerate registered accounts
        return None

    except Exception as e:
        print(f"Error creating user: {str(e)}")
        raise


def deactivate_user(email: str) -> bool:
    # Soft delete — marks user as inactive rather than physically deleting.
    # Financial systems need audit trails — hard deletes lose history.
    # Same philosophy as TTL only on PII, never on transaction records.
    try:
        table = get_users_table()
        table.update_item(
            Key={"email": email.lower()},
            UpdateExpression="SET is_active = :false, updated_at = :now",
            ConditionExpression="attribute_exists(email)",
            ExpressionAttributeValues={
                ":false": False,
                ":now": datetime.now(timezone.utc).isoformat()
            }
        )
        return True
    except Exception as e:
        print(f"Error deactivating user: {str(e)}")
        return False
