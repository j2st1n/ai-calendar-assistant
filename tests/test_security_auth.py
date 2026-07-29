import json

import pyotp
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Base, PasskeyCredential
from app.services.settings_service import SettingsService
from app.web.routes import _consume_recovery_code, _verify_totp


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


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
