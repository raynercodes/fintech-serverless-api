import pytest
import os
import src.api.core.database as database_module

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


@pytest.fixture(autouse=True)
def reset_dynamodb_singletons():
    """
    resets every cached DynamoDB singleton in database.py before and after
    each test. Without this, a table/resource object cached during one
    test's mock_aws() context can silently keep being reused by a
    LATER test, even though that later test's own fixture created a
    fresh, empty table underneath it.

    autouse=True means this applies automatically to every test in
    every file in this directory — nothing needs to import or
    reference it directly.
    """
    database_module._dynamodb = None
    database_module._transactions_table = None
    database_module._cache_table = None
    yield
    database_module._dynamodb = None
    database_module._transactions_table = None
    database_module._cache_table = None