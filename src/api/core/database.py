import boto3
import os

# Outside handler — cached in Lambda execution context (L1 cache)
# This is the same pattern as my Redis connection in Content Moderation API
# except instead of Redis client I'm caching the DynamoDB resource
_dynamodb = None
_transactions_table = None
_cache_table = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    return _dynamodb


def get_transactions_table():
    global _transactions_table
    if _transactions_table is None:
        dynamodb = get_dynamodb()
        _transactions_table = dynamodb.Table(
            os.environ["DYNAMODB_TABLE_NAME"]
        )
    return _transactions_table


def get_cache_table():
    global _cache_table
    if _cache_table is None:
        dynamodb = get_dynamodb()
        _cache_table = dynamodb.Table(
            os.environ["CACHE_TABLE_NAME"]
        )
    return _cache_table
