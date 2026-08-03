from hashlib import sha256
from base64 import urlsafe_b64encode
from typing import Any

from app.core.config import settings


def _fernet() -> Any:
    from cryptography.fernet import Fernet

    if not settings.app_secret_key:
        raise RuntimeError("APP_SECRET_KEY is not initialized")
    key = urlsafe_b64encode(sha256(settings.app_secret_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except _invalid_token_error():
        return None


def _invalid_token_error() -> type[Exception]:
    from cryptography.fernet import InvalidToken

    return InvalidToken


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••••••{value[-4:]}"
