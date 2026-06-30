import json
import time
import boto3
import os
from src.api.core.security import verify_jwt

# Outside handler — L1 cached in execution context
_dynamodb = None
_cache_table = None

# Progressive lockout windows in seconds
# Same pattern as Content Moderation API — 15min → 30min → 1hr → 24hr
LOCKOUT_WINDOWS = [900, 1800, 3600, 86400]
MAX_ATTEMPTS = 5


def get_cache_table():
    global _dynamodb, _cache_table
    if _cache_table is None:
        _dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _cache_table = _dynamodb.Table(os.environ["CACHE_TABLE_NAME"])
    return _cache_table


def get_lockout_key(identifier: str) -> str:
    return f"brute_force:{identifier}"


def check_lockout(identifier: str) -> tuple[bool, int]:
    try:
        table = get_cache_table()
        result = table.get_item(Key={"cache_key": get_lockout_key(identifier)})
        item = result.get("Item")

        if not item:
            return False, 0

        # Explicit TTL evaluation — same pattern as PII expiry
        if item.get("expires_at") and item["expires_at"] < int(time.time()):
            return False, 0

        attempts = int(item.get("attempts", 0)) + 1
        locked_until = int(item.get("locked_until", 0))

        # Check if currently locked out
        if locked_until > int(time.time()):
            remaining = locked_until - int(time.time())
            return True, remaining

        return False, attempts

    except Exception as e:
        print(f"Lockout check failed: {str(e)}")
        return False, 0


def record_failed_attempt(identifier: str):
    try:
        table = get_cache_table()
        key = get_lockout_key(identifier)

        result = table.get_item(Key={"cache_key": key})
        item = result.get("Item")

        now = int(time.time())

        if not item or (item.get("expires_at") and item["expires_at"] < now):
            # First failed attempt
            attempts = 1
            locked_until = 0
        else:
            attempts = int(item.get("attempts", 0)) + 1
            locked_until = 0

        # Progressive lockout — same windows as Content Moderation API
        # 5 attempts → 15min, 6 → 30min, 7 → 1hr, 8+ → 24hr
        if attempts >= MAX_ATTEMPTS:
            lockout_index = min(attempts - MAX_ATTEMPTS, len(LOCKOUT_WINDOWS) - 1)
            lockout_duration = LOCKOUT_WINDOWS[lockout_index]
            locked_until = now + lockout_duration
            expires_at = now + lockout_duration + 60
            print(f"Account locked: {identifier} — attempts: {attempts} — locked for {lockout_duration}s")
        else:
            expires_at = now + 86400
            print(f"Failed attempt recorded: {identifier} — attempts: {attempts}/{MAX_ATTEMPTS}")

        table.put_item(Item={
            "cache_key": key,
            "identifier": identifier,
            "attempts": attempts,
            "locked_until": locked_until,
            "expires_at": expires_at,
            "last_attempt": now
        })

    except Exception as e:
        print(f"Failed to record attempt: {str(e)}")


def clear_failed_attempts(identifier: str):
    try:
        table = get_cache_table()
        table.delete_item(Key={"cache_key": get_lockout_key(identifier)})
    except Exception as e:
        print(f"Failed to clear attempts: {str(e)}")


def generate_policy(principal_id: str, effect: str, resource: str, context: dict = None) -> dict:
    policy = {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource
                }
            ]
        }
    }
    if context:
        policy["context"] = context
    return policy


def handler(event, context):
    try:
        auth_header = event.get("authorizationToken", "")

        if not auth_header.startswith("Bearer "):
            return generate_policy("unauthorized", "Deny", event["methodArn"])

        token = auth_header.split(" ")[1]

        # Decode token to get identifier for lockout tracking
        # I check lockout before full verification to save compute
        import base64
        try:
            # Decode JWT payload without verification just to get account_id
            payload_b64 = token.split(".")[1]
            # Add padding if needed
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload_data = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
            identifier = payload_data.get("account_id", token[:16])
        except Exception:
            identifier = token[:16]

        # Check if this identifier is locked out
        is_locked, remaining_or_attempts = check_lockout(identifier)

        if is_locked:
            remaining = remaining_or_attempts
            minutes = remaining // 60
            seconds = remaining % 60
            print(f"Blocked locked account: {identifier} — {minutes}m {seconds}s remaining")
            return generate_policy("locked", "Deny", event["methodArn"])

        # Full JWT verification
        payload = verify_jwt(token)

        if payload is None:
            # Record failed attempt
            record_failed_attempt(identifier)
            return generate_policy("unauthorized", "Deny", event["methodArn"])

        # Successful auth — clear any previous failed attempts
        account_id = payload.get("account_id", "unknown")
        customer_id = payload.get("customer_id", "unknown")

        clear_failed_attempts(account_id)

        context_data = {
            "account_id": account_id,
            "customer_id": customer_id
        }

        return generate_policy(
            account_id,
            "Allow",
            event["methodArn"],
            context_data
        )

    except Exception:
        return generate_policy("unauthorized", "Deny", event["methodArn"])
