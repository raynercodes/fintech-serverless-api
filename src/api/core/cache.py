import os
import json
import time
from src.api.core.database import get_cache_table

# L1 — Lambda execution context memory cache
# Same concept as caching my Redis connection outside the handler
# in my Content Moderation API — lives in memory across warm invocations
_l1_cache = {}


def l1_get(key: str):
    entry = _l1_cache.get(key)
    if not entry:
        return None
    # Explicit TTL evaluation — never trust time-based expiry implicitly
    if entry["expires_at"] < time.time():
        del _l1_cache[key]
        return None
    return entry["value"]


def l1_set(key: str, value, ttl_seconds: int = 300):
    _l1_cache[key] = {
        "value": value,
        "expires_at": time.time() + ttl_seconds
    }


def l1_delete(key: str):
    _l1_cache.pop(key, None)


# L2 — DynamoDB shared cache (survives across Lambda containers)
# Same concept as my Redis caching in Content Moderation API
# except DynamoDB instead of Redis — no always-on cost floor
def l2_get(key: str):
    try:
        table = get_cache_table()
        result = table.get_item(Key={"cache_key": key})
        item = result.get("Item")
        if not item:
            return None
        # Explicit TTL evaluation — same pattern as PII expiry check
        # DynamoDB TTL deletion can lag up to 48 hours
        if item.get("expires_at") and item["expires_at"] < int(time.time()):
            return None
        return json.loads(item["value"])
    except Exception:
        return None


def l2_set(key: str, value, ttl_seconds: int = 300):
    try:
        table = get_cache_table()
        table.put_item(Item={
            "cache_key": key,
            "value": json.dumps(value),
            "expires_at": int(time.time()) + ttl_seconds
        })
    except Exception:
        pass


def l2_delete(key: str):
    try:
        table = get_cache_table()
        table.delete_item(Key={"cache_key": key})
    except Exception:
        pass


# Combined cache lookup — L1 first, L2 fallback
# Same logic as my Redis BLPOP pattern — check fastest source first
def cache_get(key: str):
    value = l1_get(key)
    if value is not None:
        return value
    value = l2_get(key)
    if value is not None:
        l1_set(key, value)
    return value


def cache_set(key: str, value, ttl_seconds: int = 300):
    l1_set(key, value, ttl_seconds)
    l2_set(key, value, ttl_seconds)

# function to delete cache from both L1 and L2
def cache_delete(key: str):
    l1_delete(key)
    l2_delete(key)
