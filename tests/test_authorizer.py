import pytest
import boto3
import json
import os
import time
from moto import mock_aws
from unittest.mock import patch
from jose import jwt

# Set environment variables before imports
os.environ["CACHE_TABLE_NAME"] = "fintech-cache-test"
os.environ["SQS_QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/123456789/fintech-test.fifo"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["FUNCTION_NAME"] = "fintech-api-test"
os.environ["APP_VERSION"] = "1.0.0"
os.environ["ENVIRONMENT"] = "test"

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only"

@pytest.fixture
def cache_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="fintech-cache-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "cache_key", "AttributeType": "S"}
            ],
            KeySchema=[
                {"AttributeName": "cache_key", "KeyType": "HASH"}
            ]
        )
        yield table


def make_jwt(account_id: str, customer_id: str, secret: str = TEST_JWT_SECRET, expired: bool = False):
    now = int(time.time())
    payload = {
        "account_id": account_id,
        "customer_id": customer_id,
        "iat": now,
        "exp": now - 10 if expired else now + 3600
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def make_authorizer_event(token: str, method_arn: str = "arn:aws:execute-api:us-east-1:123456789:test/dev/GET/loans"):
    return {
        "authorizationToken": f"Bearer {token}",
        "methodArn": method_arn
    }


# -------------------------------------------------------
# JWT Tests
# -------------------------------------------------------

def test_valid_jwt_returns_allow(cache_table):
    from src.authorizer.handler import handler

    token = make_jwt("acc_001", "cust_001")
    event = make_authorizer_event(token)

    with patch("src.authorizer.handler.verify_jwt") as mock_verify:
        mock_verify.return_value = {
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
        result = handler(event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert result["principalId"] == "acc_001"
    assert result["context"]["account_id"] == "acc_001"
    assert result["context"]["customer_id"] == "cust_001"


def test_invalid_jwt_returns_deny(cache_table):
    from src.authorizer.handler import handler

    event = make_authorizer_event("invalid.token.here")

    with patch("src.authorizer.handler.verify_jwt") as mock_verify:
        mock_verify.return_value = None
        result = handler(event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_missing_bearer_prefix_returns_deny(cache_table):
    from src.authorizer.handler import handler

    event = {
        "authorizationToken": "notabearer token",
        "methodArn": "arn:aws:execute-api:us-east-1:123456789:test/dev/GET/loans"
    }
    result = handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


def test_empty_token_returns_deny(cache_table):
    from src.authorizer.handler import handler

    event = {
        "authorizationToken": "",
        "methodArn": "arn:aws:execute-api:us-east-1:123456789:test/dev/GET/loans"
    }
    result = handler(event, None)
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


# -------------------------------------------------------
# Brute Force Tests
# -------------------------------------------------------

def test_brute_force_no_lockout_before_max_attempts(cache_table):
    from src.authorizer.handler import (
        record_failed_attempt,
        check_lockout,
        clear_failed_attempts,
        MAX_ATTEMPTS
    )

    identifier = "acc_brute_001"
    clear_failed_attempts(identifier)

    for i in range(MAX_ATTEMPTS - 1):
        record_failed_attempt(identifier)
        is_locked, _ = check_lockout(identifier)
        assert not is_locked, f"Should not be locked after {i + 1} attempts"


def test_brute_force_locks_after_max_attempts(cache_table):
    from src.authorizer.handler import (
        record_failed_attempt,
        check_lockout,
        clear_failed_attempts,
        MAX_ATTEMPTS,
        LOCKOUT_WINDOWS
    )

    identifier = "acc_brute_002"
    clear_failed_attempts(identifier)

    for i in range(MAX_ATTEMPTS):
        record_failed_attempt(identifier)

    is_locked, remaining = check_lockout(identifier)
    assert is_locked, "Should be locked after MAX_ATTEMPTS failures"
    assert remaining > 0, "Should have remaining lockout time"
    assert remaining <= LOCKOUT_WINDOWS[0], "Should be in first lockout window"


def test_brute_force_clears_on_successful_auth(cache_table):
    from src.authorizer.handler import (
        record_failed_attempt,
        check_lockout,
        clear_failed_attempts,
        MAX_ATTEMPTS
    )

    identifier = "acc_brute_003"
    clear_failed_attempts(identifier)

    for i in range(MAX_ATTEMPTS - 1):
        record_failed_attempt(identifier)

    clear_failed_attempts(identifier)
    is_locked, _ = check_lockout(identifier)
    assert not is_locked, "Should not be locked after clearing"


def test_brute_force_progressive_escalation(cache_table):
    from src.authorizer.handler import (
        record_failed_attempt,
        check_lockout,
        clear_failed_attempts,
        MAX_ATTEMPTS,
        LOCKOUT_WINDOWS
    )

    identifier = "acc_brute_004"
    clear_failed_attempts(identifier)

    # First lockout window
    for i in range(MAX_ATTEMPTS):
        record_failed_attempt(identifier)

    is_locked, remaining = check_lockout(identifier)
    assert is_locked
    assert remaining <= LOCKOUT_WINDOWS[0]

    # Reset and hit second lockout window
    clear_failed_attempts(identifier)
    for i in range(MAX_ATTEMPTS + 1):
        record_failed_attempt(identifier)

    is_locked, remaining = check_lockout(identifier)
    assert is_locked
    assert remaining <= LOCKOUT_WINDOWS[1]


def test_locked_account_denied_in_handler(cache_table):
    from src.authorizer.handler import (
        handler,
        record_failed_attempt,
        clear_failed_attempts,
        MAX_ATTEMPTS
    )

    identifier = "acc_brute_005"
    clear_failed_attempts(identifier)

    token = make_jwt(identifier, "cust_001")
    event = make_authorizer_event(token)

    # Lock the account
    for i in range(MAX_ATTEMPTS):
        record_failed_attempt(identifier)

    with patch("src.authorizer.handler.verify_jwt") as mock_verify:
        mock_verify.return_value = {
            "account_id": identifier,
            "customer_id": "cust_001"
        }
        result = handler(event, None)

    assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
    assert result["principalId"] == "locked"
