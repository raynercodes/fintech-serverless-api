import pytest
import boto3
import json
import os
from moto import mock_aws
from fastapi.testclient import TestClient
from unittest.mock import patch

# Set environment variables before imports
os.environ["DYNAMODB_TABLE_NAME"] = "fintech-transactions-test"
os.environ["CACHE_TABLE_NAME"] = "fintech-cache-test"
os.environ["USERS_TABLE_NAME"] = "fintech-users-test"
os.environ["SQS_QUEUE_URL"] = "https://sqs.us-east-1.amazonaws.com/123456789/fintech-test.fifo"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["FUNCTION_NAME"] = "fintech-api-test"
os.environ["APP_VERSION"] = "1.0.0"
os.environ["ENVIRONMENT"] = "test"

from src.api.main import app

client = TestClient(app)


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


@pytest.fixture
def users_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="fintech-users-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "email", "AttributeType": "S"}
            ],
            KeySchema=[
                {"AttributeName": "email", "KeyType": "HASH"}
            ]
        )
        yield table


# -------------------------------------------------------
# Registration Tests
# -------------------------------------------------------

@mock_aws
def test_register_success(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        response = client.post(
            "/auth/register",
            json={
                "email": "test@example.com",
                "password": "TestPass1",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "test@example.com"
        assert data["account_id"] == "acc_001"
        assert data["customer_id"] == "cust_001"
        assert "created_at" in data
        assert "password_hash" not in data


@mock_aws
def test_register_duplicate_email(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Register first time
        client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "password": "TestPass1",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Register second time with same email
        response = client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "password": "TestPass1",
                "account_id": "acc_002",
                "customer_id": "cust_002"
            }
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "Registration unsuccessful"


@mock_aws
def test_register_weak_password_no_uppercase(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "testpass1",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


@mock_aws
def test_register_weak_password_no_number(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "TestPassword",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


@mock_aws
def test_register_invalid_email(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "notanemail",
            "password": "TestPass1",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


# -------------------------------------------------------
# Login Tests
# -------------------------------------------------------

@mock_aws
def test_login_success(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper, \
         patch("src.api.core.security.get_jwt_secret") as mock_secret:
        mock_pepper.return_value = "test-pepper-value"
        mock_secret.return_value = "test-jwt-secret"

        # Register first
        client.post(
            "/auth/register",
            json={
                "email": "login@example.com",
                "password": "TestPass1",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Login
        response = client.post(
            "/auth/login",
            json={
                "email": "login@example.com",
                "password": "TestPass1"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900
        assert data["account_id"] == "acc_001"
        assert data["customer_id"] == "cust_001"


@mock_aws
def test_login_wrong_password(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Register first
        client.post(
            "/auth/register",
            json={
                "email": "wrongpass@example.com",
                "password": "TestPass1",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Login with wrong password
        response = client.post(
            "/auth/login",
            json={
                "email": "wrongpass@example.com",
                "password": "WrongPass1"
            }
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"


@mock_aws
def test_login_nonexistent_email(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        response = client.post(
            "/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "TestPass1"
            }
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"


@mock_aws
def test_login_uniform_error_message(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Wrong email
        response_bad_email = client.post(
            "/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "TestPass1"
            }
        )

        # Register then wrong password
        client.post(
            "/auth/register",
            json={
                "email": "real@example.com",
                "password": "TestPass1",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        response_bad_password = client.post(
            "/auth/login",
            json={
                "email": "real@example.com",
                "password": "WrongPass1"
            }
        )

        # Both failure modes must return identical messages
        # This is the uniform error response security test
        assert response_bad_email.json()["detail"] == response_bad_password.json()["detail"]
        assert response_bad_email.status_code == response_bad_password.status_code


# -------------------------------------------------------
# Demo Endpoint Tests
# -------------------------------------------------------

@mock_aws
def test_demo_returns_token(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper, \
         patch("src.api.core.security.get_jwt_secret") as mock_secret:
        mock_pepper.return_value = "test-pepper-value"
        mock_secret.return_value = "test-jwt-secret"

        response = client.post("/auth/demo")

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900
        assert data["account_id"] == "acc_demo_001"
        assert data["customer_id"] == "cust_demo_001"


@mock_aws
def test_demo_self_healing(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper, \
         patch("src.api.core.security.get_jwt_secret") as mock_secret:
        mock_pepper.return_value = "test-pepper-value"
        mock_secret.return_value = "test-jwt-secret"

        # Hit demo twice — second call should reuse existing account
        response1 = client.post("/auth/demo")
        response2 = client.post("/auth/demo")

        assert response1.status_code == 200
        assert response2.status_code == 200
        # Both return same account IDs
        assert response1.json()["account_id"] == response2.json()["account_id"]
