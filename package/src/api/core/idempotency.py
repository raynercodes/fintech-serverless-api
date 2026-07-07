import hashlib
import json
import time
from src.api.core.database import get_cache_table

_cache_table = None


def get_table():
    global _cache_table
    if _cache_table is None:
        _cache_table = get_cache_table()
    return _cache_table


def compute_content_hash(account_id: str, customer_id: str, amount: float, loan_type: str, description: str, credit_score: int) -> str:
    """
    Fingerprints a loan application's actual content — same idea as a
    checksum. Two submissions with IDENTICAL values for every one of
    these fields produce the exact same hash; change even one character
    in the description and you get a completely different hash.

    sort_keys=True is what makes this deterministic — without it, Python
    dicts don't guarantee key order, so the same data could theoretically
    hash differently between two calls. Sorting first means "same content,
    always same hash," no matter what.
    """
    canonical = json.dumps({
        "account_id": account_id,
        "customer_id": customer_id,
        "amount": amount,
        "type": loan_type,
        "description": description or "",
        "credit_score": credit_score
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def claim_duplicate_check(content_hash: str, transaction_id: str, window_seconds: int = 90) -> bool:
    """
    Same "guest list" pattern as before, but now fully automatic —
    keyed by the CONTENT hash instead of anything the caller supplies.

    window_seconds=90 is the deliberate short leash: long enough to
    catch a double-click or a phone retrying a dropped connection,
    short enough that if someone genuinely submits the SAME account
    for the SAME amount again tomorrow, it's correctly treated as a
    brand new, legitimate transaction — not silently blocked forever.
    """
    table = get_table()
    cache_key = f"dup_check:{content_hash}"
    expires_at = int(time.time()) + window_seconds

    try:
        table.put_item(
            Item={
                "cache_key": cache_key,
                "transaction_id": transaction_id,
                "expires_at": expires_at
            },
            ConditionExpression="attribute_not_exists(cache_key)"
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def get_duplicate_transaction_id(content_hash: str) -> str | None:
    table = get_table()
    cache_key = f"dup_check:{content_hash}"

    result = table.get_item(Key={"cache_key": cache_key})
    item = result.get("Item")

    if not item:
        return None

    if item.get("expires_at") and item["expires_at"] < int(time.time()):
        return None

    return item["transaction_id"]
