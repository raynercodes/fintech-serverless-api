import os
import json
import base64
import boto3
from jose import jwt, JWTError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from datetime import datetime, timezone
import secrets

# Outside handler — L1 cached in Lambda execution context
# Only fetched from Secrets Manager on cold start
_jwt_secret = None
_encryption_key = None


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
