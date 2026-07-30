import asyncio
import json

import pyotp
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import Base, PasskeyCredential
from app.services.settings_service import SettingsService
from app.web.routes import _consume_recovery_code, _verify_totp, setup_totp


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/console/security/totp/setup",
            "headers": [],
            "session": {},
        }
    )


def test_totp_code_cannot_be_replayed() -> None:
    previous_secret = settings.app_secret_key
    settings.app_secret_key = "test-secret"
    try:
        session = _session()
        service = SettingsService(session)
        secret = pyotp.random_base32()
        service.set("admin_totp_secret", secret, encrypted=True)
        service.set("admin_totp_last_counter", "-1")
        service.commit()
        code = pyotp.TOTP(secret).now()
        assert _verify_totp(service, code) is True
        assert _verify_totp(service, code) is False
    finally:
        settings.app_secret_key = previous_secret


def test_recovery_code_is_hashed_and_single_use() -> None:
    session = _session()
    service = SettingsService(session)
    import hashlib

    code = "abcd1234-ef567890"
    service.set("admin_recovery_code_hashes", json.dumps([hashlib.sha256(code.encode()).hexdigest()]))
    service.commit()
    assert _consume_recovery_code(service, code) is True
    assert _consume_recovery_code(service, code) is False


def test_passkey_credential_table_is_created() -> None:
    session = _session()
    session.add(PasskeyCredential(name="Laptop", credential_id="credential", public_key="public"))
    session.commit()
    assert session.scalar(select(PasskeyCredential)).name == "Laptop"


def test_totp_setup_rejects_password_before_generating_secret() -> None:
    session = _session()
    service = SettingsService(session)
    service.set("admin_password_hash", hash_password("correct-password"))
    service.commit()
    request = _request()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(setup_totp(request, "wrong-password", session, None))

    assert exc_info.value.status_code == 403
    assert "pending_totp_secret" not in request.session


def test_totp_setup_generates_secret_only_after_password_verification() -> None:
    session = _session()
    service = SettingsService(session)
    service.set("admin_username", "admin")
    service.set("admin_password_hash", hash_password("correct-password"))
    service.commit()
    request = _request()

    response = asyncio.run(setup_totp(request, "correct-password", session, None))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["secret"] == request.session["pending_totp_secret"]
    assert payload["qr_image"].startswith("data:image/png;base64,")
    assert request.session["pending_totp_started_at"] > 0
