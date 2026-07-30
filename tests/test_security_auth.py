import asyncio
import json
import time
from types import SimpleNamespace

import pyotp
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db import session as db_session
from app.db.models import Base, PasskeyCredential
from app.services.settings_service import SettingsService
from app.web.routes import (
    _consume_recovery_code,
    _verify_totp,
    passkey_registration_options,
    setup_totp,
    verify_passkey_registration,
)


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


def _json_request(payload: dict[str, object]) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/console/security/passkeys/register/options",
            "headers": [(b"content-type", b"application/json")],
            "session": {},
        },
        receive,
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
    row = session.scalar(select(PasskeyCredential))
    assert row.name == "Laptop"
    assert row.transports == "[]"


def test_passkey_migration_adds_transports_to_legacy_table(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE passkey_credentials (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(db_session, "engine", engine)

    db_session._migrate_passkey_credentials()

    columns = {column["name"] for column in inspect(engine).get_columns("passkey_credentials")}
    assert "transports" in columns
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO passkey_credentials (id) VALUES (1)"))
        assert connection.execute(
            text("SELECT transports FROM passkey_credentials WHERE id = 1")
        ).scalar_one() == "[]"


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


def test_passkey_registration_options_support_webauthn_3_helpers() -> None:
    previous_origin = settings.public_origin
    previous_rp_id = settings.webauthn_rp_id
    settings.public_origin = "https://calendar.example.com"
    settings.webauthn_rp_id = "calendar.example.com"
    try:
        session = _session()
        service = SettingsService(session)
        service.set("admin_username", "admin")
        service.set("admin_password_hash", hash_password("correct-password"))
        service.commit()
        request = _json_request({"name": "Laptop", "current_password": "correct-password"})

        response = asyncio.run(passkey_registration_options(request, session, None))
        payload = json.loads(response.body)

        assert response.status_code == 200
        assert payload["rp"]["id"] == "calendar.example.com"
        assert payload["challenge"]
        assert request.session["passkey_registration_challenge"]
    finally:
        settings.public_origin = previous_origin
        settings.webauthn_rp_id = previous_rp_id


def test_passkey_registration_persists_authenticator_transports(monkeypatch) -> None:
    previous_origin = settings.public_origin
    previous_rp_id = settings.webauthn_rp_id
    settings.public_origin = "https://calendar.example.com"
    settings.webauthn_rp_id = "calendar.example.com"
    monkeypatch.setattr(
        "webauthn.verify_registration_response",
        lambda **_kwargs: SimpleNamespace(
            credential_id=b"credential-id",
            credential_public_key=b"public-key",
            sign_count=0,
        ),
    )
    try:
        session = _session()
        request = _json_request(
            {
                "id": "credential",
                "response": {
                    "clientDataJSON": "data",
                    "attestationObject": "data",
                    "transports": ["internal", "hybrid"],
                },
            }
        )
        request.session["passkey_registration_challenge"] = "Y2hhbGxlbmdl"
        request.session["passkey_registration_name"] = "Phone"
        request.session["passkey_registration_started_at"] = int(time.time())

        response = asyncio.run(verify_passkey_registration(request, session, None))
        row = session.scalar(select(PasskeyCredential))

        assert response.status_code == 200
        assert json.loads(row.transports) == ["internal", "hybrid"]
    finally:
        settings.public_origin = previous_origin
        settings.webauthn_rp_id = previous_rp_id
