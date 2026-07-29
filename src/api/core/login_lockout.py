# deliberately separate from the
# authorizer's brute-force logic, since it protects a different
# endpoint against a different attack, keyed by a different identifier
import time
from src.api.core.database import get_cache_table
import os
import boto3

MAX_ATTEMPTS = 5
# Explicit stage list — deliberately NOT a flat list of integers.
# Mixing a non-timed "verification" stage into a list of seconds would
# recreate the exact index/sentinel confusion that cost real debugging
# Each stage says plainly what kind it is.
EMAIL_STAGES = [
    {"type": "timed", "seconds": 900},
    {"type": "timed", "seconds": 1800},
    {"type": "verification"},
    {"type": "timed", "seconds": 3600},
    {"type": "timed", "seconds": 86400},
]
# Deliberately NO verification stage — an IP has no single inbox to
# prove ownership of. Escalating a shared/attacking IP should always
# mean "wait longer," never "email whoever happens to be logging in
# right now," since that person's own account may be entirely innocent.
IP_STAGES = [
    {"type": "timed", "seconds": 900},
    {"type": "timed", "seconds": 1800},
    {"type": "timed", "seconds": 3600},
    {"type": "timed", "seconds": 86400},
]
EMAIL_ALERT_AT_LOCKOUT_COUNT = 3  # Tier 1 — only after the 3rd lockout

_ses_client = None


def _get_ses_client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("ses", region_name="us-east-1")
    return _ses_client

def _send_verification_email(email: str, source_ip: str) -> None:
    ses = _get_ses_client()
    from_address = os.environ.get("SES_FROM_ADDRESS", "alerts@security.raynercodes.dev")

    subject = "Action Required — Please Verify Your Identity to Continue"
    body_text = (
        "We noticed repeated failed login attempts on your account, "
        f"originating from IP address {source_ip}. To help protect your "
        "information, we've temporarily paused login access until you "
        "verify it's really you.\n\n"
        "If you did not attempt to log in recently, we recommend changing "
        "your password once you regain access.\n\n"
        "This is an automated security notification. If you did not request "
        "this, no further action is needed — your account remains protected.\n\n"
        "— RaynerCodes Security Team"
    )

    try:
        ses.send_email(
            Source=from_address,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}}
            }
        )
    except Exception as e:
        # Deliberately swallowed, not raised. The actual security action —
        # requires_verification written to DynamoDB — already succeeded
        # and is durable BEFORE this function ever runs. A transient SES
        # failure is a real but SEPARATE problem from protecting the
        # account; it should never roll back or block the lockout itself.
        print(f"[SES SEND FAILED] Could not notify {email}: {e}")


def _get_record(identifier_key: str) -> dict:
    table = get_cache_table()
    print(f"DEBUG — table object id: {id(table)}, table name: {table.table_name}")
    all_items = table.scan()
    print(f"DEBUG — full table contents as seen by app code: {all_items.get('Items')}")
    result = table.get_item(Key={"cache_key": identifier_key})
    return result.get("Item", {})

def _is_locked(record: dict) -> bool:
    if record.get("requires_verification"):
        return True  # Tier 2 — locked indefinitely until verification link is clicked
    return int(time.time()) < record.get("locked_until", 0)

def check_login_lockout(email: str, source_ip: str) -> bool:
    """
    Checks BOTH identifiers independently — either one being locked
    is enough to block the attempt. This is what actually closes the
    gap I found: a targeted attack trips the email lock even if the
    attacker rotates IPs; a distributed low-and-slow attack trips the
    IP lock even though no single email ever crosses its own threshold.
    """
    email_record = _get_record(f"login_lockout:email:{email}")
    ip_record = _get_record(f"login_lockout:ip:{source_ip}")
    return _is_locked(email_record) or _is_locked(ip_record)


def _record_failure_for_identifier(identifier_key: str, stages: list) -> dict:
    """
    Returns info about what just happened, so the caller can decide
    whether to trigger the combined alert+verification email. Tracks
    LOCKOUT COUNT explicitly, separate from ATTEMPTS — attempts resets
    whenever a timed window has genuinely expired (a legitimate user
    who waited it out gets real fresh tries), but lockout_count NEVER
    resets on its own — it's the one thing tracking permanent severity
    across the whole history of this identifier, which is exactly what
    should keep escalating through the stages regardless of how many
    times attempts itself gets reset along the way.
    """
    record = _get_record(identifier_key)
    now = int(time.time())

    window_has_expired = "locked_until" in record and int(record["locked_until"]) <= now
    attempts = 1 if window_has_expired else int(record.get("attempts", 0)) + 1

    lockout_count = int(record.get("lockout_count", 0))
    just_locked = False
    just_escalated = False

    if attempts > MAX_ATTEMPTS:
        lockout_count += 1
        just_locked = True

        stage = stages[min(lockout_count - 1, len(stages) - 1)]

        if stage["type"] == "verification":
            just_escalated = True
            get_cache_table().put_item(Item={
                "cache_key": identifier_key,
                "attempts": attempts,
                "lockout_count": lockout_count,
                "requires_verification": True,
                "expires_at": now + 86400 * 7
            })
        else:
            get_cache_table().put_item(Item={
                "cache_key": identifier_key,
                "attempts": attempts,
                "lockout_count": lockout_count,
                "locked_until": now + stage["seconds"],
                "expires_at": now + 86400 * 7
            })
    else:
        get_cache_table().put_item(Item={
            "cache_key": identifier_key,
            "attempts": attempts,
            "lockout_count": lockout_count,
            "expires_at": now + 86400 * 7
        })

    return {"just_locked": just_locked,
            "just_escalated": just_escalated,
            "lockout_count": lockout_count
    }


def record_failed_login(email: str, source_ip: str) -> dict:
    email_result = _record_failure_for_identifier(f"login_lockout:email:{email}", EMAIL_STAGES)
    ip_result = _record_failure_for_identifier(f"login_lockout:ip:{source_ip}", IP_STAGES)

    # ONLY the email identifier can ever trigger a verification email —
    # an IP escalating never sends anything to anyone, since there's no
    # single account it's fair to blame. It still blocks the login (via
    # check_login_lockout's OR logic), just through a longer timed wait,
    # never through emailing whoever's request happened to trip it.
    if email_result["just_escalated"]:
        _send_verification_email(email)

    return email_result


def clear_login_attempts(email: str, source_ip: str) -> None:
    """
    Called on a SUCCESSFUL login. Fully deletes both records — this is
    the one path that resets lockout_count back to zero,
    since a real successful authentication is a different, stronger
    signal than a verification-link click (which only proves email
    ownership, not that the failures have stopped).
    """
    table = get_cache_table()
    table.delete_item(Key={"cache_key": f"login_lockout:email:{email}"})
    table.delete_item(Key={"cache_key": f"login_lockout:ip:{source_ip}"})


def clear_verification_requirement(email: str) -> None:
    """
    Called once a real verification-link endpoint confirms the user
    proved ownership of their email (that endpoint doesn't exist yet —
    genuinely out of scope right now alongside the real SES send).
    deliberately does NOT touch lockout_count — proving email ownership doesn't erase this
    identifier's real history. If it fails again later, it correctly
    resumes at Stage 4 (1hr), not restarts at Stage 1.
    """
    table = get_cache_table()
    key = f"login_lockout:email:{email}"
    record = _get_record(key)
    if record.get("requires_verification"):
        now = int(time.time())
        table.put_item(Item={
            "cache_key": key,
            "attempts": 0,
            "lockout_count": int(record.get("lockout_count", 2)),
            "expires_at": now + 86400 * 7
        })
