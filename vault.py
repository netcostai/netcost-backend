import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# Retrieve the master key we just generated
MASTER_KEY = os.getenv("ENCRYPTION_MASTER_KEY")
if not MASTER_KEY:
    raise ValueError("ENCRYPTION_MASTER_KEY is missing from environment configuration.")

cipher_suite = Fernet(MASTER_KEY.encode())

def encrypt_key(plain_text_api_key: str) -> str:
    """Encrypts a client's API key to a secure AES-256 string."""
    if not plain_text_api_key:
        return ""
    encrypted_bytes = cipher_suite.encrypt(plain_text_api_key.encode())
    return encrypted_bytes.decode()

def decrypt_key(encrypted_api_key_str: str) -> str:
    """Decrypts an AES-256 string back to plain text for upstream routing."""
    if not encrypted_api_key_str:
        return ""
    decrypted_bytes = cipher_suite.decrypt(encrypted_api_key_str.encode())
    return decrypted_bytes.decode()
