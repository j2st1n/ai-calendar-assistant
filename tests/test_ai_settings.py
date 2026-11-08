import asyncio
import json
import re
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
    ai_settings,
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
    assert 'data-endpoint="/console/ai/models"' in template
    assert 'data-endpoint="/console/ai/vision-models"' in template
    assert "body: new FormData(form)" in template
    assert 'name="available_models_raw"' in template
    assert 'name="vision_available_models_raw"' in template
    assert template.count('role="combobox"') == 2
    assert template.count('role="listbox"') == 2
    assert "aria-activedescendant" in template
    assert "data-model-toggle" in template
    assert "<datalist" not in template
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


APP_DIR = Path(__file__).parents[1]
TEMPLATES_DIR = APP_DIR / "app/web/templates"
STYLES_PATH = APP_DIR / "app/web/static/styles.css"


def _render_ai_page(settings_values: dict[str, str]) -> str:
    """通过真实路由渲染 /console/ai，用于断言服务端下发的初始 DOM 状态。"""

    async def run() -> str:
        session = _session()
        service = SettingsService(session)
        for key, value in settings_values.items():
            service.set(key, value)
        service.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/console/ai",
                "headers": [],
                "query_string": b"",
                "session": {"admin_authenticated": True},
            }
        )
        response = await ai_settings(request, session=session, _=None)
        return response.body.decode()

    return asyncio.run(run())


def _tag_with_id(markup: str, tag: str, element_id: str) -> str:
    match = re.search(rf'<{tag}\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', markup)
    assert match is not None, f'缺少 <{tag} id="{element_id}">'
    return match.group(0)


def _has_hidden_attribute(tag: str) -> bool:
    return re.search(r"\shidden(?=[\s>])", tag) is not None


def _display_none_hidden_rules(css: str) -> list[tuple[int, str, str]]:
    """返回针对 [hidden] 且声明 display:none 的规则：(样式表位置, 选择器, 声明)。"""
    normalized = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[int, str, str]] = []
    for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<declarations>[^}]*)\}", normalized):
        declarations = match.group("declarations")
        if not re.search(r"display\s*:\s*none\b", declarations):
            continue
        for selector in match.group("selectors").split(","):
            selector = selector.strip()
            if "[hidden]" in selector:
                rules.append((match.start(), selector, declarations))
    return rules


def _targets_vision_card(selector: str) -> bool:
    if "#vision-card" in selector:
        return True
    return re.fullmatch(r"(?:\*|html|body|:root)?\s*\[hidden\]", selector) is not None


def test_stylesheet_keeps_hidden_attribute_authoritative_over_stack_grid() -> None:
    """回归：.stack{display:grid} 曾覆盖浏览器默认 [hidden]，导致 #vision-card 始终显示。"""
    css_text = STYLES_PATH.read_text(encoding="utf-8")

    stack_rules = [
        match
        for match in re.finditer(r"\.stack\s*\{[^}]*\}", re.sub(r"/\*.*?\*/", "", css_text, flags=re.S))
        if re.search(r"display\s*:\s*grid", match.group(0))
    ]
    assert stack_rules, ".stack 的 grid 布局定义应保留"
    stack_position = max(match.start() for match in stack_rules)

    candidates = [
        (position, selector, declarations)
        for position, selector, declarations in _display_none_hidden_rules(css_text)
        if _targets_vision_card(selector)
    ]
    assert candidates, "样式表中缺少让 [hidden] 生效的 display:none 规则"

    effective = [
        (position, selector, declarations)
        for position, selector, declarations in candidates
        # 以下任一情况都能压过 .stack{display:grid}：
        # 1) #vision-card[hidden] 的 id 选择器优先级更高；
        # 2) 全局 [hidden] 规则带 !important；
        # 3) 全局 [hidden] 与 .stack 同优先级，但声明在样式表中更靠后。
        if "#vision-card" in selector
        or re.search(r"!\s*important", declarations)
        or position > stack_position
    ]
    assert effective, (
        "缺少可压过 .stack{display:grid} 的 [hidden] 规则："
        "应使用全局 [hidden]{display:none !important} 或 #vision-card[hidden]{display:none}"
    )


def test_vision_card_visibility_follows_vision_use_main_toggle() -> None:
    """回归：勾选「用于图片识别」时卡片由 hidden 隐藏，取消勾选后由 toggleVision() 重新显示。"""
    template = (TEMPLATES_DIR / "ai.html").read_text(encoding="utf-8")

    card_tag = _tag_with_id(template, "section", "vision-card")
    assert "card stack" in card_tag
    assert _has_hidden_attribute(card_tag), "#vision-card 应保留服务端渲染的 hidden 属性"
    assert re.search(r"function toggleVision\(\)[\s\S]*?card\.hidden = useMain", template)
    assert re.search(r"toggleVision\(\);", template), "初始化时应同步一次卡片可见性"

    checkbox_tag = _tag_with_id(template, "input", "vision-use-main")
    assert 'onchange="toggleVision()"' in checkbox_tag

    enabled_page = _render_ai_page({"ai_vision_use_main": "1"})
    assert _has_hidden_attribute(_tag_with_id(enabled_page, "section", "vision-card"))
    assert "checked" in _tag_with_id(enabled_page, "input", "vision-use-main")

    disabled_page = _render_ai_page({"ai_vision_use_main": "false"})
    assert _has_hidden_attribute(_tag_with_id(disabled_page, "section", "vision-card"))
    assert "checked" not in _tag_with_id(disabled_page, "input", "vision-use-main")
    # 未勾选时 toggleVision() 将 card.hidden 置为 false，卡片显示且表单可提交到原保存路由
    assert 'action="/console/ai/vision"' in disabled_page
    assert "保存识图模型" in disabled_page
    assert re.search(r"Boolean\(checkbox && checkbox\.checked\)", template)
