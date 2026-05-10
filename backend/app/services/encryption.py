"""Simple encrypt/decrypt using Fernet. Falls back to plaintext if no key configured."""
import os
from cryptography.fernet import Fernet
from ..config import settings


def generate_key() -> bytes:
    return Fernet.generate_key()


def _get_key() -> bytes | None:
    key = settings.encryption_key
    if not key:
        key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        return None
    return key.encode() if isinstance(key, str) else key


def _is_enabled() -> bool:
    return _get_key() is not None


def encrypt(plaintext: str) -> str:
    key = _get_key()
    if key is None:
        return "PLAIN:" + plaintext
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    if token.startswith("PLAIN:"):
        return token[6:]
    key = _get_key()
    if key is None:
        return token
    f = Fernet(key)
    return f.decrypt(token.encode()).decode()


def mask_key(key: str) -> str:
    if key.startswith("PLAIN:"):
        key = key[6:]
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]
