from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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


settings = Settings()
