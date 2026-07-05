import pytest
import boto3
import json
import os
from moto import mock_aws
from fastapi.testclient import TestClient
from unittest.mock import patch
from jose import jwt as jose_jwt
import time
from src.worker.handler import determine_loan_status, process_transaction

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only"

# Set environment variables before importing the app
# Must happen before any imports that read os.environ
os.environ["DYNAMODB_TABLE_NAME"] = "fintech-transactions-test"
os.environ["CACHE_TABLE_NAME"] = "fintech-cache-test"
os.environ["SQS_QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/123456789/fintech-test.fifo"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

from src.api.main import app

client = TestClient(app)


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


@pytest.fixture
def dynamodb_tables(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

        # Create transactions table
        transactions_table = dynamodb.create_table(
            TableName="fintech-transactions-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "account_id", "AttributeType": "S"},
                {"AttributeName": "timestamp_transaction_id", "AttributeType": "S"},
                {"AttributeName": "transaction_id", "AttributeType": "S"},
                {"AttributeName": "customer_id", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "account_id", "KeyType": "HASH"},
                {"AttributeName": "timestamp_transaction_id", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "transaction-id-index",
                    "KeySchema": [
                        {"AttributeName": "transaction_id", "KeyType": "HASH"}
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "customer-id-index",
                    "KeySchema": [
                        {"AttributeName": "customer_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp_transaction_id", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )

        # Create cache table
        cache_table = dynamodb.create_table(
            TableName="fintech-cache-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "cache_key", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "cache_key", "KeyType": "HASH"},
            ],
        )

        yield transactions_table, cache_table


@mock_aws
def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

@pytest.fixture
def auth_headers():
    # Generate a valid test JWT — same structure the authorizer expects
    # Bypasses real Secrets Manager — test-only secret
    now = int(time.time())
    payload = {
        "account_id": "acc_001",
        "customer_id": "cust_001",
        "iat": now,
        "exp": now + 3600
    }
    token = jose_jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}

@mock_aws
def test_submit_loan_application(dynamodb_tables, auth_headers):
    with patch("src.api.routes.loans.get_sqs") as mock_sqs:
        mock_sqs.return_value.send_message.return_value = {
            "MessageId": "test-message-id"
        }

        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 5000.00,
                "type": "deposit",
                "credit_score": 700,
                "description": "Initial loan deposit"
            },
            headers=auth_headers
        )

        assert response.status_code == 201
        data = response.json()
        assert data["account_id"] == "acc_001"
        assert data["customer_id"] == "cust_001"
        assert data["amount"] == 5000.00
        assert data["status"] == "pending"
        assert "transaction_id" in data
        assert "timestamp" in data


@mock_aws
def test_submit_loan_invalid_amount(dynamodb_tables, auth_headers):
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": -500.00,
                "type": "deposit",
                "credit_score": 700
            },
            headers=auth_headers
        )
        assert response.status_code == 422


@mock_aws
def test_submit_loan_invalid_type(dynamodb_tables, auth_headers):
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 1000.00,
                "type": "invalid_type",
                "credit_score": 700
            },
            headers=auth_headers
        )
        assert response.status_code == 422


@mock_aws
def test_get_loan_not_found(dynamodb_tables, auth_headers):
    response = client.get("/loans/nonexistent-id", headers=auth_headers)
    assert response.status_code == 404


@mock_aws
def test_get_loans_by_account_empty(dynamodb_tables, auth_headers):
    response = client.get("/loans/account/acc_999", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


@mock_aws
def test_get_loans_by_customer_empty(dynamodb_tables, auth_headers):
    response = client.get("/loans/customer/cust_999", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


# ============================================================
# NEW — credit_score validation at the Pydantic layer
# These confirm the "bouncer at the door" actually rejects
# bad scores BEFORE they reach SQS or the worker at all.
# ============================================================

@mock_aws
def test_submit_loan_credit_score_too_low(dynamodb_tables, auth_headers):
    """A score below 300 isn't a real FICO score — Pydantic should reject it."""
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 1000.00,
                "type": "deposit",
                "credit_score": 250  # below the ge=300 floor
            },
            headers=auth_headers
        )
        assert response.status_code == 422


@mock_aws
def test_submit_loan_credit_score_too_high(dynamodb_tables, auth_headers):
    """A score above 850 isn't a real FICO score either — same bouncer, other direction."""
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 1000.00,
                "type": "deposit",
                "credit_score": 900  # above the le=850 ceiling
            },
            headers=auth_headers
        )
        assert response.status_code == 422


@mock_aws
def test_submit_loan_missing_credit_score(dynamodb_tables, auth_headers):
    """credit_score has no default — leaving it out entirely should 422, not silently pass."""
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 1000.00,
                "type": "deposit"
                # credit_score intentionally omitted
            },
            headers=auth_headers
        )
        assert response.status_code == 422


# ============================================================
# determine_loan_status() unit tests
#
# These don't touch DynamoDB, SQS, or FastAPI at all — no
# @mock_aws, no fixtures, no auth_headers. That's the whole
# point of pulling this into its own pure function: we can
# test the actual business rule in complete isolation.
#
# We test every tier PLUS the exact boundary numbers, since
# boundary values (exactly 650, exactly 500) are where
# off-by-one bugs (>= vs >) actually hide.
# ============================================================

def test_determine_loan_status_clearly_approved():
    assert determine_loan_status(750) == "approved"


def test_determine_loan_status_clearly_review():
    assert determine_loan_status(550) == "review"


def test_determine_loan_status_clearly_rejected():
    assert determine_loan_status(350) == "rejected"


def test_determine_loan_status_boundary_650_is_approved():
    """650 is the cutoff — the rule is >=650, so exactly 650 must be approved, not review."""
    assert determine_loan_status(650) == "approved"


def test_determine_loan_status_boundary_649_is_review():
    """One point below the cutoff — this is the number that would expose an off-by-one bug."""
    assert determine_loan_status(649) == "review"


def test_determine_loan_status_boundary_500_is_review():
    """500 is the review cutoff — exactly 500 must be review, not rejected."""
    assert determine_loan_status(500) == "review"


def test_determine_loan_status_boundary_499_is_rejected():
    """One point below the review floor — should tip into rejected."""
    assert determine_loan_status(499) == "rejected"


def test_determine_loan_status_min_score():
    """300 is the lowest real FICO score — should be rejected."""
    assert determine_loan_status(300) == "rejected"


def test_determine_loan_status_max_score():
    """850 is a perfect score — should obviously be approved."""
    assert determine_loan_status(850) == "approved"


# ============================================================
# worker Lambda integration tests
#
# These simulate an actual SQS event hitting your worker handler
# directly (no real SQS involved — we just build the same JSON
# shape SQS would deliver). This proves the FULL pipeline works:
# message in -> credit score evaluated -> correct status written
# to DynamoDB.
# ============================================================

def build_sqs_event(transaction_data: dict) -> dict:
    """
    Helper — builds a fake SQS event in the exact shape AWS delivers
    to your worker's handler(event, context). Keeps the test bodies
    below clean instead of repeating this structure every time.
    """
    return {
        "Records": [
            {
                "messageId": "test-msg-id",
                "body": json.dumps(transaction_data)
            }
        ]
    }


@mock_aws
def test_worker_approves_high_credit_score(dynamodb_tables):
    transactions_table, _ = dynamodb_tables

    event = build_sqs_event({
        "transaction_id": "txn-approved-001",
        "account_id": "acc_001",
        "customer_id": "cust_001",
        "amount": 10000.00,
        "type": "deposit",
        "credit_score": 700,
        "timestamp": "2026-07-05T12:00:00.000Z",
        "description": "test loan"
    })

    # NOTE: calling process_transaction directly instead of handler(event, context)
    # because handler() calls get_table() which reaches for the REAL table name
    # via get_transactions_table() — process_transaction lets us pass the
    # mocked table in directly, matching how my other worker code is structured.
    process_transaction(transactions_table, json.loads(event["Records"][0]["body"]))

    result = transactions_table.get_item(
        Key={
            "account_id": "acc_001",
            "timestamp_transaction_id": "2026-07-05T12:00:00.000Z#txn-approved-001"
        }
    )
    assert result["Item"]["status"] == "approved"
    assert result["Item"]["credit_score"] == 700


@mock_aws
def test_worker_sends_midrange_score_to_review(dynamodb_tables):
    transactions_table, _ = dynamodb_tables

    event = build_sqs_event({
        "transaction_id": "txn-review-001",
        "account_id": "acc_001",
        "customer_id": "cust_001",
        "amount": 10000.00,
        "type": "deposit",
        "credit_score": 580,
        "timestamp": "2026-07-05T12:01:00.000Z",
        "description": "test loan"
    })

    process_transaction(transactions_table, json.loads(event["Records"][0]["body"]))

    result = transactions_table.get_item(
        Key={
            "account_id": "acc_001",
            "timestamp_transaction_id": "2026-07-05T12:01:00.000Z#txn-review-001"
        }
    )
    assert result["Item"]["status"] == "review"


@mock_aws
def test_worker_rejects_low_credit_score(dynamodb_tables):
    transactions_table, _ = dynamodb_tables

    event = build_sqs_event({
        "transaction_id": "txn-rejected-001",
        "account_id": "acc_001",
        "customer_id": "cust_001",
        "amount": 10000.00,
        "type": "deposit",
        "credit_score": 420,
        "timestamp": "2026-07-05T12:02:00.000Z",
        "description": "test loan"
    })

    process_transaction(transactions_table, json.loads(event["Records"][0]["body"]))

    result = transactions_table.get_item(
        Key={
            "account_id": "acc_001",
            "timestamp_transaction_id": "2026-07-05T12:02:00.000Z#txn-rejected-001"
        }
    )
    assert result["Item"]["status"] == "rejected"


# ============================================================
# PATCH /loans/{loan_id}/status state machine tests
#
# Strategy: instead of going through the full POST -> SQS -> worker
# pipeline just to GET a loan into a certain status, we write a
# DynamoDB item directly with put_item() at whatever status we want
# to start from. This isolates what we're actually testing (the
# PATCH endpoint's transition rules) everything starts from "review" in these tests.
# ============================================================

def seed_loan(table, transaction_id: str, account_id: str, status: str):
    """Helper — directly inserts a loan item at a specific status,
    skipping the whole POST/SQS/worker pipeline since we only care
    about testing the PATCH transition rules here."""
    table.put_item(Item={
        "account_id": account_id,
        "timestamp_transaction_id": f"2026-07-05T12:00:00.000Z#{transaction_id}",
        "transaction_id": transaction_id,
        "customer_id": "cust_001",
        "timestamp": "2026-07-05T12:00:00.000Z",
        "amount": "5000.00",
        "credit_score": 580,
        "status": status,
        "type": "deposit",
        "description": "seeded test loan",
    })


@mock_aws
def test_review_loan_can_be_approved(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-review-to-approved", "acc_001", "review")

    response = client.patch(
        "/loans/txn-review-to-approved/status",
        json={"status": "approved"},
        headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


@mock_aws
def test_review_loan_can_be_rejected(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-review-to-rejected", "acc_001", "review")

    response = client.patch(
        "/loans/txn-review-to-rejected/status",
        json={"status": "rejected"},
        headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


@mock_aws
def test_rejected_loan_cannot_be_changed(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-final-rejected", "acc_001", "rejected")

    response = client.patch(
        "/loans/txn-final-rejected/status",
        json={"status": "approved"},  # trying to un-reject it
        headers=auth_headers
    )
    assert response.status_code == 409


@mock_aws
def test_approved_loan_can_move_to_funded(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-approved-to-funded", "acc_001", "approved")

    response = client.patch(
        "/loans/txn-approved-to-funded/status",
        json={"status": "funded"},
        headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "funded"


@mock_aws
def test_approved_loan_cannot_go_back_to_review(dynamodb_tables, auth_headers):
    """This is the rule — approved can only move FORWARD (funded/repaid/defaulted),
    never backward to review/pending/rejected."""
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-approved-no-backslide", "acc_001", "approved")

    response = client.patch(
        "/loans/txn-approved-no-backslide/status",
        json={"status": "review"},
        headers=auth_headers
    )
    assert response.status_code == 409


@mock_aws
def test_funded_loan_can_be_repaid(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-funded-to-repaid", "acc_001", "funded")

    response = client.patch(
        "/loans/txn-funded-to-repaid/status",
        json={"status": "repaid"},
        headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "repaid"


@mock_aws
def test_repaid_loan_is_final(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-final-repaid", "acc_001", "repaid")

    response = client.patch(
        "/loans/txn-final-repaid/status",
        json={"status": "defaulted"},
        headers=auth_headers
    )
    assert response.status_code == 409


@mock_aws
def test_defaulted_loan_is_final(dynamodb_tables, auth_headers):
    transactions_table, _ = dynamodb_tables
    seed_loan(transactions_table, "txn-final-defaulted", "acc_001", "defaulted")

    response = client.patch(
        "/loans/txn-final-defaulted/status",
        json={"status": "funded"},
        headers=auth_headers
    )
    assert response.status_code == 409
