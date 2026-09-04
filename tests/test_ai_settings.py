import asyncio
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.db.models import Base
from app.services.ai_provider_service import AIProviderConfig, AIProviderError
from app.services.settings_service import SettingsService
from app.web.routes import (
    _normalize_url,
    _probe_api_key,
    _validated_provider_type,
    pull_ai_models,
    update_ai_settings,
)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/console/ai", "headers": [], "session": {}})


def test_main_and_vision_forms_use_native_controls_and_draft_probes() -> None:
    template = (Path(__file__).parents[1] / "app/web/templates/ai.html").read_text()

    assert '<select name="provider_name"' in template
    assert '<select name="vision_provider_name"' in template
    assert 'type="button" class="secondary" data-endpoint="/console/ai/models"' in template
    assert 'type="button" class="secondary" data-endpoint="/console/ai/vision-models"' in template
    assert "body: new FormData(form)" in template
    assert 'name="available_models_raw"' in template
    assert 'name="vision_available_models_raw"' in template
    routes = (Path(__file__).parents[1] / "app/web/routes.py").read_text()
    assert 'request.session["ai_models"]' not in routes
    assert 'request.session["ai_vision_models"]' not in routes


def test_update_ai_settings_preserves_available_models_and_can_disable_main_vision() -> None:
    async def run() -> None:
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_provider_name", "Custom")
        settings.commit()

        response = await update_ai_settings(
            _request(),
            provider_name="Custom",
            provider_type="openai_compatible",
            base_url="http://127.0.0.1:9000/v1/",
            api_key="",
            clear_api_key="",
            model="model-b",
            available_models_raw="model-a, model-b",
            vision_use_main="false",
            session=session,
            _=None,
        )

        assert response.status_code == 303
        assert settings.get("ai_base_url") == "http://127.0.0.1:9000/v1"
        assert settings.get("ai_model") == "model-b"
        assert settings.get("ai_available_models") == "model-a,model-b"
        assert settings.get("ai_vision_use_main") == "false"

    asyncio.run(run())


def test_switching_provider_clears_old_key_and_derives_known_protocol() -> None:
    async def run() -> None:
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_provider_name", "OpenAI")
        settings.set("ai_api_key", "old-openai-key")
        settings.commit()

        await update_ai_settings(
            _request(),
            provider_name="Anthropic",
            provider_type="openai_compatible",
            base_url="https://api.anthropic.com",
            api_key="",
            clear_api_key="",
            model="claude-test",
            available_models_raw="claude-test",
            vision_use_main="1",
            session=session,
            _=None,
        )

        assert settings.get("ai_provider_type") == "anthropic"
        assert settings.get("ai_api_key") is None

    asyncio.run(run())


def test_pull_models_uses_draft_without_mutating_saved_settings(monkeypatch) -> None:
    captured: dict[str, AIProviderConfig] = {}

    class FakeProviderService:
        async def list_models(self, config: AIProviderConfig) -> list[str]:
            captured["config"] = config
            return ["draft-model-a", "draft-model-b"]

    async def run() -> None:
        session = _session()
        settings = SettingsService(session)
        settings.set("ai_provider_name", "OpenAI")
        settings.set("ai_provider_type", "openai_compatible")
        settings.set("ai_base_url", "https://api.openai.com/v1")
        settings.set("ai_model", "saved-model")
        settings.set("ai_available_models", "saved-model")
        settings.commit()

        monkeypatch.setattr(
            "app.web.routes._ai_components",
            lambda: (AIProviderConfig, AIProviderError, FakeProviderService),
        )
        response = await pull_ai_models(
            _request(),
            provider_name="Custom",
            provider_type="openai_compatible",
            base_url="http://127.0.0.1:9000/v1",
            api_key="draft-key",
            clear_api_key="",
            session=session,
            _=None,
        )

        assert response.status_code == 200
        assert json.loads(response.body)["models"] == ["draft-model-a", "draft-model-b"]
        assert captured["config"].base_url == "http://127.0.0.1:9000/v1"
        assert captured["config"].api_key == "draft-key"
        assert settings.get("ai_provider_name") == "OpenAI"
        assert settings.get("ai_base_url") == "https://api.openai.com/v1"
        assert settings.get("ai_model") == "saved-model"
        assert settings.get("ai_available_models") == "saved-model"

    asyncio.run(run())


def test_probe_does_not_reuse_key_after_provider_change() -> None:
    session = _session()
    settings = SettingsService(session)
    settings.set("ai_provider_name", "OpenAI")
    settings.set("ai_provider_type", "openai_compatible")
    settings.set("ai_api_key", "old-provider-key")
    settings.commit()

    assert _probe_api_key(
        settings,
        submitted_key="",
        submitted_provider_name="Anthropic",
        provider_setting="ai_provider_name",
        key_setting="ai_api_key",
    ) == ""
    assert _probe_api_key(
        settings,
        submitted_key="",
        submitted_provider_name="OpenAI",
        provider_setting="ai_provider_name",
        key_setting="ai_api_key",
    ) == "old-provider-key"
    assert _probe_api_key(
        settings,
        submitted_key="",
        submitted_provider_name="OpenAI",
        provider_setting="ai_provider_name",
        key_setting="ai_api_key",
        submitted_provider_type="anthropic",
        type_setting="ai_provider_type",
    ) == ""


def test_normalize_url_validates_scheme_host_and_credentials() -> None:
    assert _normalize_url("example.com/v1/") == "https://example.com/v1"
    assert _normalize_url("http://127.0.0.1:9000/v1/") == "http://127.0.0.1:9000/v1"
    for invalid in (
        "",
        "ftp://example.com",
        "http://",
        "https://example.com:bad",
        "https://user:pass@example.com",
    ):
        try:
            _normalize_url(invalid)
        except ValueError:
            continue
        raise AssertionError(f"expected invalid URL: {invalid}")


def test_known_provider_protocol_cannot_be_overridden_by_form_data() -> None:
    assert _validated_provider_type("anthropic", "OpenAI") == "openai_compatible"
    assert _validated_provider_type("openai_compatible", "Anthropic") == "anthropic"
    assert _validated_provider_type("anthropic", "Custom") == "anthropic"
