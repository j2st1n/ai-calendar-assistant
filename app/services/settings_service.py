from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import Setting


class SettingsService:
    session: Session

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> str | None:
        row = self.session.get(Setting, key)
        if row is None:
            return None
        if row.encrypted:
            return decrypt_secret(row.value)
        return row.value

    def get_masked(self, key: str) -> str:
        row = self.session.get(Setting, key)
        if row is None or row.value is None:
            return ""
        if row.encrypted:
            return mask_secret(decrypt_secret(row.value))
        return row.value

    def set(self, key: str, value: str | None, encrypted: bool = False) -> None:
        stored_value = encrypt_secret(value) if encrypted else value
        row = self.session.get(Setting, key)
        if row is None:
            self.session.add(Setting(key=key, value=stored_value, encrypted=encrypted))
            return
        row.value = stored_value
        row.encrypted = encrypted
        row.updated_at = datetime.now(timezone.utc)

    def commit(self) -> None:
        self.session.commit()
