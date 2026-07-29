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
        cache_table = dynamodb.create_table(
            TableName="fintech-cache-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "cache_key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "cache_key", "KeyType": "HASH"}]
        )
        yield table


# -------------------------------------------------------
# Registration Tests
# -------------------------------------------------------

def test_register_success(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        response = client.post(
            "/auth/register",
            json={
                "email": "test@example.com",
                "password": "TestPass1!",
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


def test_register_duplicate_email(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Register first time
        client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "password": "TestPass1!",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Register second time with same email
        response = client.post(
            "/auth/register",
            json={
                "email": "duplicate@example.com",
                "password": "TestPass1!",
                "account_id": "acc_002",
                "customer_id": "cust_002"
            }
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "Registration unsuccessful"


def test_register_weak_password_no_uppercase(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "testpass1!",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


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


def test_register_invalid_email(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "notanemail",
            "password": "TestPass1!",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


# -------------------------------------------------------
# Login Tests
# -------------------------------------------------------


def test_login_route_locks_out_after_repeated_failures(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        client.post(
            "/auth/register",
            json={
                "email": "bruteforce@example.com",
                "password": "TestPass1!",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Fail past MAX_ATTEMPTS using the WRONG password, through the
        # REAL route — not calling login_lockout.py directly
        for _ in range(6):
            response = client.post(
                "/auth/login",
                json={"email": "bruteforce@example.com", "password": "WrongPassword1!"}
            )

        # This 6th response should already be a lockout, not just
        # another "wrong password" rejection
        assert response.status_code == 401

        # THE critical assertion — try again with the CORRECT password.
        # If lockout is genuinely wired into the route, this must still
        # fail, since the account is locked regardless of credentials.
        response = client.post(
            "/auth/login",
            json={"email": "bruteforce@example.com", "password": "TestPass1!"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"


# ============================================================
# This is the other half of the brute-force defense: an attacker
# spraying different emails from ONE IP should get locked out at
# the IP level, even though no single email ever crosses its own
# threshold. Proves the actual security value of the dual design,
# not just that each identifier works in isolation.
# ============================================================

def test_login_route_locks_out_ip_across_different_emails(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Register 6 distinct, real accounts — simulates an attacker
        # who has a list of valid emails and is trying each one once,
        # rather than hammering a single victim
        for i in range(6):
            client.post(
                "/auth/register",
                json={
                    "email": f"target{i}@example.com",
                    "password": "RealPassword1!",
                    "account_id": f"acc_{i}",
                    "customer_id": f"cust_{i}"
                }
            )

        # Attacker tries each account exactly ONCE, wrong password,
        # all from the SAME simulated source IP. TestClient doesn't
        # let us directly set a custom source IP through normal
        # request params, so we patch request.client.host at the
        # FastAPI level to simulate one consistent attacking IP.
        with patch("starlette.requests.Request.client") as mock_client:
            mock_client.host = "6.6.6.6"

            response = None
            for i in range(6):
                response = client.post(
                    "/auth/login",
                    json={"email": f"target{i}@example.com", "password": "WrongPassword1!"}
                )

            # By the 6th distinct email, no INDIVIDUAL email has hit
            # MAX_ATTEMPTS — but the shared IP has now failed 6 times
            assert response.status_code == 401

            # THE key assertion — a 7th, completely fresh, never-before-
            # seen email, same attacking IP, CORRECT password this time.
            # Must still be blocked, proving the IP lock (not any single
            # email's lock) is what's actually catching this pattern.
            client.post(
                "/auth/register",
                json={
                    "email": "brand-new-target@example.com",
                    "password": "CorrectPassword1!",
                    "account_id": "acc_new",
                    "customer_id": "cust_new"
                }
            )
            response = client.post(
                "/auth/login",
                json={"email": "brand-new-target@example.com", "password": "CorrectPassword1!"}
            )
            assert response.status_code == 401
            assert response.json()["detail"] == "Invalid credentials"


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
                "password": "TestPass1!",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        # Login
        response = client.post(
            "/auth/login",
            json={
                "email": "login@example.com",
                "password": "TestPass1!"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "15 minutes" in data["expires_in"]
        assert data["account_id"] == "acc_001"
        assert data["customer_id"] == "cust_001"


def test_login_wrong_password(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Register first
        client.post(
            "/auth/register",
            json={
                "email": "wrongpass@example.com",
                "password": "TestPass1!",
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


def test_login_nonexistent_email(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        response = client.post(
            "/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "TestPass1!"
            }
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"


def test_register_weak_password_no_lowercase(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "TESTPASS1!",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


def test_register_weak_password_no_special_character(users_table):
    response = client.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "TestPass1",
            "account_id": "acc_001",
            "customer_id": "cust_001"
        }
    )
    assert response.status_code == 422


def test_login_uniform_error_message(users_table):
    with patch("src.api.core.security.get_password_pepper") as mock_pepper:
        mock_pepper.return_value = "test-pepper-value"

        # Wrong email
        response_bad_email = client.post(
            "/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "TestPass1!"
            }
        )

        # Register then wrong password
        client.post(
            "/auth/register",
            json={
                "email": "real@example.com",
                "password": "TestPass1!",
                "account_id": "acc_001",
                "customer_id": "cust_001"
            }
        )

        response_bad_password = client.post(
            "/auth/login",
            json={
                "email": "real@example.com",
                "password": "WrongPass1!"
            }
        )

        # Both failure modes must return identical messages
        # This is the uniform error response security test
        assert response_bad_email.json()["detail"] == response_bad_password.json()["detail"]
        assert response_bad_email.status_code == response_bad_password.status_code


# -------------------------------------------------------
# Demo Endpoint Tests
# -------------------------------------------------------

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
        assert "15 minutes" in data["expires_in"]
        assert data["account_id"] == "acc_demo_001"
        assert data["customer_id"] == "cust_demo_001"


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
