from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 9527
    app_secret_key: str | None = None
    database_url: str = "sqlite:///data/app.db"
    admin_username: str = "admin"
    admin_password: str | None = None
    session_days: int = 7
    event_record_limit: int = 500
    data_dir: str = "data"
    public_origin: str = ""
    webauthn_rp_id: str = ""
    trusted_hosts: str = "127.0.0.1,localhost,testserver"
    trust_proxy_headers: bool = False
    secure_cookies: bool = False

    @model_validator(mode="after")
    def resolve_paths(self) -> "Settings":
        data_path = Path(self.data_dir)
        if not data_path.is_absolute():
            self.data_dir = str((BASE_DIR / data_path).resolve())

        if (
            self.database_url.startswith("sqlite:///")
            and not self.database_url.startswith("sqlite:////")
            and self.database_url not in ("sqlite://", "sqlite:///:memory:", "sqlite://?mode=memory&cache=shared")
        ):
            rel_part = self.database_url[len("sqlite:///"):]
            if "?" in rel_part:
                path_part, query_part = rel_part.split("?", 1)
                db_path = Path(path_part)
                if not db_path.is_absolute():
                    resolved_path = (BASE_DIR / db_path).resolve()
                    self.database_url = f"sqlite:///{resolved_path}?{query_part}"
            else:
                db_path = Path(rel_part)
                if not db_path.is_absolute():
                    resolved_path = (BASE_DIR / db_path).resolve()
                    self.database_url = f"sqlite:///{resolved_path}"

        return self


settings = Settings()
