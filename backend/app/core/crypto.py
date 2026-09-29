"""
Symmetric encryption for integration OAuth tokens at rest. Uses Fernet
(AES-128-CBC + HMAC, authenticated) — appropriate for this use case: we
need to decrypt tokens server-side to make outbound calendar API calls,
so this is encryption-at-rest, not hashing.

TOKEN_ENCRYPTION_KEY must be a urlsafe-base64 32-byte key. Generate with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set — cannot encrypt/decrypt integration tokens.")
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
