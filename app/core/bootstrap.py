import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, init_db
from app.services.settings_service import SettingsService


def bootstrap_application() -> None:
    data_dir = Path(settings.data_dir)
    ensure_data_dir(data_dir)
    ensure_app_secret(data_dir / "secrets.json")
    init_db()
    with SessionLocal() as session:
        settings_service = SettingsService(session)
        ensure_admin(settings_service)
        ensure_default_settings(settings_service)


def ensure_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError as exc:
        print(f"Warning: could not set secure permissions on {data_dir}: {exc}", flush=True)


def ensure_app_secret(secrets_path: Path) -> None:
    if secrets_path.exists():
        try:
            payload = json.loads(secrets_path.read_text())
            app_secret_key = payload.get("app_secret_key")
        except (json.JSONDecodeError, OSError):
            app_secret_key = None
        if not app_secret_key:
            app_secret_key = secrets.token_urlsafe(48)
            secrets_path.write_text(
                json.dumps(
                    {
                        "app_secret_key": app_secret_key,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    indent=2,
                )
            )
        settings.app_secret_key = app_secret_key
        try:
            os.chmod(secrets_path, 0o600)
        except OSError as exc:
            print(f"Warning: could not set secure permissions on {secrets_path}: {exc}", flush=True)
        return

    app_secret_key = settings.app_secret_key or secrets.token_urlsafe(48)
    payload = {
        "app_secret_key": app_secret_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    secrets_path.write_text(json.dumps(payload, indent=2))
    settings.app_secret_key = app_secret_key
    try:
        os.chmod(secrets_path, 0o600)
    except OSError as exc:
        print(f"Warning: could not set secure permissions on {secrets_path}: {exc}", flush=True)


def ensure_admin(settings_service: SettingsService) -> None:
    username = settings_service.get("admin_username")
    password_hash = settings_service.get("admin_password_hash")
    if username and password_hash:
        print_initialized_message()
        return

    admin_username = settings.admin_username or "admin"
    admin_password = settings.admin_password or generate_password()
    settings_service.set("admin_username", admin_username)
    settings_service.set("admin_password_hash", hash_password(admin_password))
    settings_service.set("admin_password_changed", "false")
    settings_service.commit()
    print_initial_credentials(admin_username, admin_password)


def ensure_default_settings(settings_service: SettingsService) -> None:
    defaults = {
        "session_days": str(settings.session_days),
        "event_record_limit": str(settings.event_record_limit),
    }
    for key, value in defaults.items():
        if settings_service.get(key) is None:
            settings_service.set(key, value)
    settings_service.commit()


def read_version() -> str:
    try:
        return Path("VERSION").read_text().strip()
    except Exception:
        return "dev"


def read_changes() -> dict[str, Any]:
    try:
        lines = Path("CHANGELOG.md").read_text().splitlines()
    except Exception:
        return {"version": "", "sections": []}

    version = ""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_latest = False
    for line in lines:
        if line.startswith("## "):
            if in_latest:
                break
            in_latest = True
            version = line.lstrip("# ").strip()
            continue
        if not in_latest:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            current = {"title": stripped.lstrip("# ").strip(), "entries": []}
            sections.append(current)
            continue
        if stripped.startswith("- "):
            if current is None:
                current = {"title": "Changes", "entries": []}
                sections.append(current)
            current["entries"].append(stripped[2:].strip())
    return {"version": version, "sections": sections}


def generate_password() -> str:
    token = secrets.token_urlsafe(18).replace("_", "-")
    return "-".join([token[i : i + 6] for i in range(0, min(len(token), 24), 6)])


def print_initial_credentials(username: str, password: str) -> None:
    print("=" * 58, flush=True)
    print("AI Calendar Assistant initialized", flush=True)
    print(f"Web UI: http://127.0.0.1:{settings.app_port}", flush=True)
    print(f"Username: {username}", flush=True)
    print(f"Password: {password}", flush=True)
    print("Please change this password in System Settings.", flush=True)
    print("=" * 58, flush=True)


def print_initialized_message() -> None:
    print("AI Calendar Assistant already initialized.", flush=True)
    print(f"Web UI: http://127.0.0.1:{settings.app_port}", flush=True)
