from cryptography.fernet import Fernet
import os
import sys
import secrets
import hashlib

raw_key = os.getenv("ENCRYPTION_MASTER_KEY")

if not raw_key:
    print("FATAL: ENCRYPTION_MASTER_KEY is not set. Refusing to start.")
    sys.exit(1)

try:
    cipher_suite = Fernet(raw_key.strip().encode())
except Exception as e:
    print(f"FATAL: ENCRYPTION_MASTER_KEY is invalid: {e}. Refusing to start.")
    sys.exit(1)


def encrypt_key(key: str) -> str:
    return cipher_suite.encrypt(key.encode()).decode()


def decrypt_key(encrypted_key: str) -> str:
    return cipher_suite.decrypt(encrypted_key.encode()).decode()


def generate_api_key() -> str:
    """A secret, unguessable key issued to a company at onboarding."""
    return f"nc_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """We only ever store this hash — never the raw key."""
    return hashlib.sha256(api_key.encode()).hexdigest()