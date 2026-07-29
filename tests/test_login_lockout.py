import pytest
import boto3
import os
import time
from moto import mock_aws
from unittest.mock import patch

os.environ["CACHE_TABLE_NAME"] = "fintech-cache-test"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

from src.api.core.login_lockout import (
    check_login_lockout, record_failed_login, clear_login_attempts,
    clear_verification_requirement, MAX_ATTEMPTS, IP_STAGES, EMAIL_STAGES
)
import src.api.core.database as database_module


@pytest.fixture
def cache_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="fintech-cache-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "cache_key", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "cache_key", "KeyType": "HASH"}],
        )
        yield table


@pytest.fixture(autouse=True)
def reset_dynamodb_singletons():
    database_module._dynamodb = None
    database_module._transactions_table = None
    database_module._cache_table = None
    yield
    database_module._dynamodb = None
    database_module._transactions_table = None
    database_module._cache_table = None


def _fail_n_times(email, ip, n):
    result = None
    for _ in range(n):
        result = record_failed_login(email, ip)
    return result


# ============================================================
# Boundary — unchanged core rule, still correct in the new design
# ============================================================

def test_exactly_max_attempts_does_not_lock(cache_table):
    _fail_n_times("boundary1@example.com", "1.1.1.1", MAX_ATTEMPTS)
    assert check_login_lockout("boundary1@example.com", "1.1.1.1") is False


def test_one_past_max_attempts_locks(cache_table):
    _fail_n_times("boundary2@example.com", "1.1.1.2", MAX_ATTEMPTS + 1)
    assert check_login_lockout("boundary2@example.com", "1.1.1.2") is True


# ============================================================
# Stage progression — 15min -> 30min -> VERIFICATION, in order
# ============================================================

def test_stage1_is_15_minutes(cache_table):
    result = _fail_n_times("stage1@example.com", "2.2.2.1", MAX_ATTEMPTS + 1)
    assert result["lockout_count"] == 1
    item = cache_table.get_item(Key={"cache_key": "login_lockout:email:stage1@example.com"})["Item"]
    remaining = int(item["locked_until"]) - int(time.time())
    assert 890 < remaining <= 900

@mock_aws
@patch("src.api.core.login_lockout._send_verification_email")
def test_stage3_is_verification_not_a_timer(mock_send_email, cache_table):
    """The critical new design point — the 3rd lockout has NO
    locked_until at all, it's a hard verification gate."""
    email = "stage3@example.com"
    ip = "2.2.2.3"

    # Seed as if stages 1 and 2 already happened and expired
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS,
        "lockout_count": 2,
        "expires_at": int(time.time()) + 86400
    })

    result = record_failed_login(email, ip)
    assert result["lockout_count"] == 3
    assert result["just_escalated"] is True

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    assert item["requires_verification"] is True
    assert "locked_until" not in item
    assert check_login_lockout(email, ip) is True


def test_stage4_resumes_timed_after_verification_stage(cache_table):
    """Confirms stages 4/5 (1hr, 24hr) still exist AFTER verification —
    the sequence continues, it doesn't stop at verification forever."""
    email = "stage4@example.com"
    ip = "2.2.2.4"

    # Seed as if verification was already cleared (attempts reset,
    # lockout_count preserved at 3 — exactly what clear_verification_requirement does)
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS,
        "lockout_count": 3,
        "expires_at": int(time.time()) + 86400
    })

    result = record_failed_login(email, ip)
    assert result["lockout_count"] == 4

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    remaining = int(item["locked_until"]) - int(time.time())
    assert 3590 < remaining <= 3600  # ~1 hour, Stage 4


def test_beyond_final_stage_clamps_to_24hr_repeatedly(cache_table):
    email = "stage6@example.com"
    ip = "2.2.2.6"
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS,
        "lockout_count": len(EMAIL_STAGES) + 3,  # well past every real stage
        "expires_at": int(time.time()) + 86400
    })
    result = record_failed_login(email, ip)
    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    remaining = int(item["locked_until"]) - int(time.time())
    assert 86390 < remaining <= 86400  # still the 24hr stage, clamped


# ============================================================
# THE key fix from tonight's whole debugging session — attempts
# resets after a genuinely expired TIMED window, but lockout_count
# does NOT. This is the exact behavior gap the original test design
# incorrectly assumed already existed.
# ============================================================

def test_attempts_resets_after_expired_window_but_lockout_count_does_not(cache_table):
    email = "reset-check@example.com"
    ip = "3.3.3.1"

    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS + 1,
        "lockout_count": 1,
        "locked_until": 0,  # already expired
        "expires_at": int(time.time()) + 86400
    })

    result = record_failed_login(email, ip)
    assert result["lockout_count"] == 1  # still just 1 more failure, no NEW lockout yet

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    assert int(item["attempts"]) == 1  # reset to a fresh count, not 7

@mock_aws
@patch("src.api.core.login_lockout._send_verification_email")
def test_lockout_count_never_resets_from_a_single_expired_window(mock_send_email, cache_table):
    """Proves lockout_count keeps its full history even while attempts
    keeps resetting — this is what makes stage progression correct
    across multiple separate episodes over time."""
    email = "history-check@example.com"
    ip = "3.3.3.2"

    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": 1,
        "lockout_count": 2,
        "locked_until": 0,
        "expires_at": int(time.time()) + 86400
    })

    result = _fail_n_times(email, ip, MAX_ATTEMPTS + 1)
    assert result["lockout_count"] == 3  # correctly climbs from 2, not reset to 1


# ============================================================
# Dual identifier — unchanged design, still confirmed
# ============================================================

def test_email_lockout_blocks_even_from_new_ip(cache_table):
    _fail_n_times("victim@example.com", "4.4.4.1", MAX_ATTEMPTS + 1)
    assert check_login_lockout("victim@example.com", "9.9.9.9") is True


def test_ip_lockout_blocks_even_new_email(cache_table):
    attacker_ip = "4.4.4.2"
    for email in ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com", "f@x.com"]:
        record_failed_login(email, attacker_ip)
    assert check_login_lockout("never-seen-before@x.com", attacker_ip) is True

def test_ip_escalation_never_sets_requires_verification(cache_table):
    """The core fix — even pushed all the way to what WOULD be the
    verification stage for an email, an IP just gets a longer timed
    lockout instead. Never requires_verification."""
    attacker_ip = "8.8.8.1"
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:ip:{attacker_ip}",
        "attempts": MAX_ATTEMPTS,
        "lockout_count": 2,
        "expires_at": int(time.time()) + 86400
    })

    record_failed_login("innocent-bystander@example.com", attacker_ip)

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:ip:{attacker_ip}"})["Item"]
    assert "requires_verification" not in item
    assert "locked_until" in item  # a real timed wait instead


@mock_aws
@patch("src.api.core.login_lockout._send_verification_email")
def test_ip_escalation_does_not_email_innocent_user(mock_send_email, cache_table):
    """Directly proves the bug you found — an innocent user whose OWN
    email has never failed shouldn't get a verification email just
    because they share an IP with an attacker."""
    attacker_ip = "8.8.8.2"
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:ip:{attacker_ip}",
        "attempts": MAX_ATTEMPTS,
        "lockout_count": 2,
        "expires_at": int(time.time()) + 86400
    })

    record_failed_login("truly-innocent@example.com", attacker_ip)

    mock_send_email.assert_not_called()


# ============================================================
# Successful login — full reset, both identifiers
# ============================================================

def test_successful_login_clears_both_records_entirely(cache_table):
    record_failed_login("clear-test@example.com", "5.5.5.1")
    clear_login_attempts("clear-test@example.com", "5.5.5.1")

    assert check_login_lockout("clear-test@example.com", "5.5.5.1") is False
    result = cache_table.get_item(Key={"cache_key": "login_lockout:email:clear-test@example.com"})
    assert "Item" not in result


# ============================================================
# Verification clearing — YOUR specific correction: resets
# attempts, but lockout_count is explicitly preserved
# ============================================================

def test_verification_resets_attempts_but_preserves_lockout_count(cache_table):
    email = "verify-check@example.com"
    ip = "6.6.6.1"

    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS + 1,
        "lockout_count": 3,
        "requires_verification": True,
        "expires_at": int(time.time()) + 86400
    })
    clear_verification_requirement(email)

    # No longer locked — verification cleared it
    assert check_login_lockout(email, ip) is False

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    assert int(item["attempts"]) == 0
    assert int(item["lockout_count"]) == 3  # NOT reset — the exact behavior you specified
    assert "requires_verification" not in item


def test_failure_after_verification_resumes_at_stage4_not_stage1(cache_table):
    """The real proof of your design: verify, then fail again later —
    must land on Stage 4 (1hr), not restart the whole sequence."""
    email = "resume-check@example.com"
    ip = "6.6.6.2"

    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS + 1,
        "lockout_count": 3,
        "requires_verification": True,
        "expires_at": int(time.time()) + 86400
    })
    clear_verification_requirement(email)

    result = _fail_n_times(email, ip, MAX_ATTEMPTS + 1)
    assert result["lockout_count"] == 4  # resumed at Stage 4, not restarted at 1


# ============================================================
# Edge case — corrupted/missing lockout_count on a
# requires_verification record. Defaults to 2, not 0, since this
# path is only ever reached for an identifier already known to be
# at the verification stage — trusting the data less, not more,
# is the safer assumption for a security control like this.
# ============================================================

def test_verification_fallback_defaults_to_2_when_lockout_count_missing(cache_table):
    email = "corrupted-data@example.com"
    ip = "7.7.7.1"

    # Deliberately seed WITHOUT lockout_count at all — simulates a
    # corrupted or manually-edited record, not something the real
    # code path would ever normally produce on its own
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS + 1,
        "requires_verification": True,
        "expires_at": int(time.time()) + 86400
        # lockout_count intentionally omitted
    })
    cache_table.put_item(Item={
        "cache_key": f"login_lockout:ip:{ip}",
        "attempts": MAX_ATTEMPTS + 1,
        "requires_verification": True,
        "expires_at": int(time.time()) + 86400
    })

    clear_verification_requirement(email)

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    assert int(item["lockout_count"]) == 2
    assert int(item["attempts"]) == 0
    assert "requires_verification" not in item


@mock_aws
@patch("src.api.core.login_lockout._send_verification_email")
def test_fallback_value_correctly_re_triggers_verification_on_next_failure(mock_send_email, cache_table):
    """
    Proves the actual math, not just the number in isolation — since
    the fallback deliberately picks 2, the very next failure after it
    should increment to 3 and land right back on the verification
    stage (STAGES[2]), not accidentally skip past it or fall short.
    """
    email = "corrupted-data-2@example.com"
    ip = "7.7.7.2"

    cache_table.put_item(Item={
        "cache_key": f"login_lockout:email:{email}",
        "attempts": MAX_ATTEMPTS + 1,
        "requires_verification": True,
        "expires_at": int(time.time()) + 86400
        # lockout_count intentionally omitted, same as above
    })

    clear_verification_requirement(email)

    result = _fail_n_times(email, ip, MAX_ATTEMPTS + 1)
    assert result["lockout_count"] == 3
    assert result["just_escalated"] is True

    item = cache_table.get_item(Key={"cache_key": f"login_lockout:email:{email}"})["Item"]
    assert item["requires_verification"] is True
