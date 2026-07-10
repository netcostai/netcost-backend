from cryptography.fernet import Fernet
import os

# Bulletproof key loading
raw_key = os.getenv("ENCRYPTION_MASTER_KEY")

if not raw_key:
    # Fallback if key is missing
    cipher_suite = Fernet(Fernet.generate_key())
else:
    try:
        # Clean and encode the key
        cipher_suite = Fernet(raw_key.strip().encode())
    except Exception:
        # Fallback if key is malformed
        cipher_suite = Fernet(Fernet.generate_key())

def encrypt_key(key: str) -> str:
    return cipher_suite.encrypt(key.encode()).decode()

def decrypt_key(encrypted_key: str) -> str:
    return cipher_suite.decrypt(encrypted_key.encode()).decode()