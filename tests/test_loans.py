import pytest
import boto3
import json
import os
from moto import mock_aws
from fastapi.testclient import TestClient
from unittest.mock import patch

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


@mock_aws
def test_submit_loan_application(dynamodb_tables):
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
                "description": "Initial loan deposit"
            }
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
def test_submit_loan_invalid_amount(dynamodb_tables):
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": -500.00,
                "type": "deposit"
            }
        )
        assert response.status_code == 422


@mock_aws
def test_submit_loan_invalid_type(dynamodb_tables):
    with patch("src.api.routes.loans.get_sqs"):
        response = client.post(
            "/loans/",
            json={
                "account_id": "acc_001",
                "customer_id": "cust_001",
                "amount": 1000.00,
                "type": "invalid_type"
            }
        )
        assert response.status_code == 422


@mock_aws
def test_get_loan_not_found(dynamodb_tables):
    response = client.get("/loans/nonexistent-id")
    assert response.status_code == 404


@mock_aws
def test_get_loans_by_account_empty(dynamodb_tables):
    response = client.get("/loans/account/acc_999")
    assert response.status_code == 200
    assert response.json() == []


@mock_aws
def test_get_loans_by_customer_empty(dynamodb_tables):
    response = client.get("/loans/customer/cust_999")
    assert response.status_code == 200
    assert response.json() == []
