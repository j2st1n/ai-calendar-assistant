import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import Setting

AI_CONFIG_KEYS: tuple[str, ...] = (
    "ai_api_key",
    "ai_available_models",
    "ai_base_url",
    "ai_model",
    "ai_provider_name",
    "ai_provider_type",
    "ai_vision_api_key",
    "ai_vision_available_models",
    "ai_vision_base_url",
    "ai_vision_model",
    "ai_vision_provider_name",
    "ai_vision_provider_type",
    "ai_vision_use_main",
)

CALDAV_CONFIG_KEYS: tuple[str, ...] = (
    "caldav_calendar_name",
    "caldav_calendar_url",
    "caldav_default_duration",
    "caldav_password",
    "caldav_reminder_minutes",
    "caldav_ssl_verify",
    "caldav_timezone",
    "caldav_url",
    "caldav_username",
)


class SettingsService:
    session: Session

    def __init__(self, session: Session) -> None:
        self.session = session

    def _get_row(self, key: str) -> Setting | None:
        row = self.session.get(Setting, key)
        if row is not None:
            return row
        for obj in self.session.new:
            if isinstance(obj, Setting) and obj.key == key:
                return obj
        return None

    def get(self, key: str) -> str | None:
        row = self._get_row(key)
        if row is None:
            return None
        if row.encrypted:
            return decrypt_secret(row.value)
        return row.value

    def get_masked(self, key: str) -> str:
        row = self._get_row(key)
        if row is None or row.value is None:
            return ""
        if row.encrypted:
            return mask_secret(decrypt_secret(row.value))
        return row.value

    def set(self, key: str, value: str | None, encrypted: bool = False) -> None:
        stored_value = encrypt_secret(value) if encrypted else value
        row = self._get_row(key)
        if row is None:
            self.session.add(Setting(key=key, value=stored_value, encrypted=encrypted))
            return
        row.value = stored_value
        row.encrypted = encrypted
        row.updated_at = datetime.now(timezone.utc)

    def _compute_hash(self, keys: tuple[str, ...]) -> str:
        pairs = []
        for k in sorted(keys):
            val = self.get(k)
            pairs.append((k, val if val is not None else ""))
        data = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def get_ai_config_hash(self) -> str:
        return self._compute_hash(AI_CONFIG_KEYS)

    def get_caldav_config_hash(self) -> str:
        return self._compute_hash(CALDAV_CONFIG_KEYS)

    def get_config_fingerprint(self) -> str:
        combined = f"{self.get_ai_config_hash()}:{self.get_caldav_config_hash()}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    def get_config_version(self) -> int:
        try:
            ver_str = self.get("_config_version")
            cur_ai = self.get_ai_config_hash()
            cur_cal = self.get_caldav_config_hash()
            last_ai = self.get("_config_ai_hash")
            last_cal = self.get("_config_caldav_hash")

            if ver_str is None:
                version = 1
                self.set("_config_version", str(version))
                self.set("_config_ai_hash", cur_ai)
                self.set("_config_caldav_hash", cur_cal)
                try:
                    self.session.flush()
                except Exception:
                    pass
                return version

            try:
                version = int(ver_str)
            except (ValueError, TypeError):
                version = 1

            if cur_ai != last_ai or cur_cal != last_cal:
                version += 1
                self.set("_config_version", str(version))
                self.set("_config_ai_hash", cur_ai)
                self.set("_config_caldav_hash", cur_cal)
                try:
                    self.session.flush()
                except Exception:
                    pass
            return version
        except Exception:
            return 1

    def sync_config_version(self) -> int:
        return self.get_config_version()

    def commit(self) -> None:
        self.get_config_version()
        self.session.commit()
