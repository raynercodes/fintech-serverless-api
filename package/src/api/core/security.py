import os
import json
import base64
import time
import boto3
from jose import jwt, JWTError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from datetime import datetime, timezone
import secrets

# Outside handler — L1 cached in Lambda execution context — fetched once per cold start
# Only fetched from Secrets Manager on cold start
_jwt_secret = None
_encryption_key = None
_password_pepper = None

# Password hashing — for user registration and login
# Uses PBKDF2-HMAC-SHA256 via the standard library's hashlib
# No external dependency needed — same algorithm family bcrypt uses
# under the hood, but built into Python so no extra package weight
# on the Lambda deployment package
import hashlib
import secrets as secrets_module


def get_password_pepper() -> str:
    global _password_pepper
    if _password_pepper is None:
        client = boto3.client("secretsmanager", region_name="us-east-1")
        response = client.get_secret_value(
            SecretId="/fintech/prod/password-pepper"
        )
        _password_pepper = json.loads(response["SecretString"])["pepper"]
    return _password_pepper

def hash_password(password: str) -> str:
    # Pepper — secret value from Secrets Manager, never stored in the database
    # Defense in depth — even a fully compromised DynamoDB table is unusable
    # without also compromising Secrets Manager separately
    pepper = get_password_pepper()
    peppered_password = password + pepper
    # Generate a random 16-byte salt — unique per password
    # Same principle as the nonce in AES-GCM encryption
    # Prevents identical passwords from producing identical hashes
    salt = secrets_module.token_bytes(16)

    # PBKDF2 — deliberately slow hashing function
    # 200,000 iterations makes brute force attacks computationally expensive
    # Each guess an attacker tries costs real CPU time, not microseconds
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        peppered_password.encode("utf-8"),
        salt,
        600_000
    )

    # Store salt + hash together, base64 encoded
    # Salt must be stored alongside the hash — you need it to verify later
    # Pepper is NEVER stored here — it lives only in Secrets Manager
    combined = salt + hashed
    return base64.b64encode(combined).decode("utf-8")


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        pepper = get_password_pepper()
        peppered_password = password + pepper

        combined = base64.b64decode(stored_hash)

        # Salt is always the first 16 bytes — same split pattern as decrypt_pii
        salt = combined[:16]
        original_hash = combined[16:]

        # Recompute the hash using the same salt that was stored
        # If the password is correct the hashes will match exactly
        new_hash = hashlib.pbkdf2_hmac(
            "sha256",
            peppered_password.encode("utf-8"),
            salt,
            600_000
        )

        # Constant-time comparison — prevents timing attacks
        # A regular == comparison can leak information about how many
        # characters matched based on how long the comparison took
        return secrets_module.compare_digest(new_hash, original_hash)
    except Exception:
        return False


def create_jwt(account_id: str, customer_id: str, expires_in_seconds: int = 900) -> str:
    secret = get_jwt_secret()
    now = int(time.time())

    # Standard JWT claims — iat (issued at) and exp (expiry)
    # Same short-lived token pattern as my Content Moderation API
    # "iat" is the issued-at timestamp, "exp" is the expiration timestamp
    payload = {
        "account_id": account_id,
        "customer_id": customer_id,
        "iat": now,
        "exp": now + expires_in_seconds
    }

    return jwt.encode(payload, secret, algorithm="HS256")

def get_jwt_secret() -> str:
    global _jwt_secret
    if _jwt_secret is None:
        client = boto3.client("secretsmanager", region_name="us-east-1")
        response = client.get_secret_value(
            SecretId="/fintech/prod/jwt-secret"
        )
        _jwt_secret = json.loads(response["SecretString"])["secret"]
    return _jwt_secret


def get_encryption_key() -> bytes:
    global _encryption_key
    if _encryption_key is None:
        client = boto3.client("secretsmanager", region_name="us-east-1")
        response = client.get_secret_value(
            SecretId="/fintech/prod/encryption-key"
        )
        key_b64 = json.loads(response["SecretString"])["key"]
        # Decode from base64 string to raw bytes
        # AES-256-GCM requires exactly 32 bytes
        _encryption_key = base64.b64decode(key_b64)
    return _encryption_key


def verify_jwt(token: str) -> dict:
    try:
        secret = get_jwt_secret()
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"]
        )
        return payload
    except JWTError:
        return None


def encrypt_pii(data: dict) -> str:
    key = get_encryption_key()
    # AESGCM is AES-256-GCM — authenticated encryption
    # Authenticated means it detects tampering, not just encrypts
    aesgcm = AESGCM(key)
    # Nonce — random 12 bytes, must be unique per encryption operation
    # Same concept as a salt in my password hashing from Content Moderation API
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(data).encode("utf-8")
    # Encrypt — returns ciphertext with authentication tag appended
    # The authentication tag is what makes GCM different from plain AES
    # it lets you detect tampering on decryption
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    # Combine nonce + ciphertext into one base64 string for DynamoDB storage
    # We need the same nonce stored alongside the ciphertext to decrypt later
    # without it decryption is impossible.
    combined = base64.b64encode(nonce + ciphertext).decode("utf-8")
    return combined


def decrypt_pii(encrypted_data: str) -> dict:
    try:
        key = get_encryption_key()
        aesgcm = AESGCM(key)
        # Decode the combined base64 string back to bytes
        combined = base64.b64decode(encrypted_data)
        # Split nonce and ciphertext — nonce is always first 12 bytes
        nonce = combined[:12]
        ciphertext = combined[12:]
        # Decrypt — raises exception if data was tampered with
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
