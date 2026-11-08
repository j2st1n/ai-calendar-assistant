from dataclasses import dataclass, field
import base64 as base64_module
import hashlib
import hmac
import importlib
import json
import secrets
from typing import Any
from urllib.parse import urlsplit
import uuid

OPENAI_COMPATIBLE_USER_AGENT = "ai-calendar-assistant"
PROBE_TIMEOUT_SECONDS = 20.0
COMPLETION_TIMEOUT_SECONDS = 60.0
CLIENT_MAX_RETRIES = 1
_EPHEMERAL_SESSION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class AIProviderConfig:
    provider_type: str
    base_url: str
    api_key: str | None
    model: str | None = None
    # Probes are independent conversations; callers with a chat context override this.
    session_id: str = field(default_factory=lambda: "cal-" + uuid.uuid4().hex)


def conversation_session_id(source: str, user_id: str, conversation_id: str | None = None) -> str:
    """An opaque, stable conversation identifier, without exposing channel/user IDs."""
    from app.core.config import settings

    key = settings.app_secret_key.encode() if settings.app_secret_key else _EPHEMERAL_SESSION_KEY
    scope = json.dumps(
        ["ai-calendar/opencode/session/v1", source, user_id, conversation_id or user_id],
        ensure_ascii=True, separators=(",", ":"),
    ).encode()
    return "cal-" + hmac.new(key, scope, hashlib.sha256).hexdigest()


def _opencode_headers(config: AIProviderConfig) -> dict[str, str]:
    try:
        url = urlsplit(config.base_url)
        official = url.scheme == "https" and url.hostname == "opencode.ai" and url.port in (None, 443)
    except ValueError:
        official = False
    if not official:
        return {}
    return {"User-Agent": OPENAI_COMPATIBLE_USER_AGENT, "x-opencode-session": config.session_id}


def safe_ai_connection_error(exc: Exception) -> str:
    """Expose actionable categories, never provider payloads, URLs or credentials."""
    chain: list[BaseException] = [exc]
    for _ in range(4):
        cause = chain[-1].__cause__
        if cause is None or cause in chain:
            break
        chain.append(cause)
    current = next((item for item in chain if isinstance(getattr(item, "status_code", None), int)), chain[-1])
    status = getattr(current, "status_code", None)
    prefix = f"AI 连接测试失败（HTTP {status}）：" if isinstance(status, int) and 100 <= status <= 599 else "AI 连接测试失败："
    # Inspect locally only; none of this untrusted text is copied into the response.
    text = str(getattr(current, "body", "")).lower() + " " + str(current).lower()
    if "x-opencode-session" in text and any(word in text for word in ("missing", "required", "provide", "include", "缺少")):
        reason = "OpenCode 要求 x-opencode-session 会话标头。"
    elif status == 401:
        reason = "API Key 无效或已失效，请检查供应商凭据。"
    elif status == 403:
        reason = "供应商拒绝访问，请检查账号权限、隐私设置或订阅适用范围。"
    elif status == 404:
        reason = "接口或模型不存在，请检查 Base URL 和模型名称。"
    elif status == 429:
        reason = "达到供应商速率或用量限制，请稍后重试或检查额度。"
    elif isinstance(status, int) and 500 <= status <= 599:
        reason = "供应商服务暂时异常，请稍后重试。"
    elif status == 400:
        reason = "请求不被供应商接受，请检查协议、模型及供应商设置。"
    elif any(type(item).__name__ in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "TimeoutError"} for item in chain):
        reason = "AI 请求超时，请稍后重试。"
    elif any(type(item).__name__ in {"APIConnectionError", "ConnectError"} for item in chain):
        reason = "无法连接 AI 接口，请检查网络和 Base URL。"
    else:
        reason = "请检查 Base URL、API Key 和模型配置。"
    return prefix + reason


class AIProviderService:
    async def chat_completion(
        self, config: AIProviderConfig, system_prompt: str, user_message: str, json_mode: bool = True
    ) -> str:
        if config.provider_type == "anthropic":
            return await self._anthropic_chat(config, system_prompt, user_message)
        return await self._openai_chat(config, system_prompt, user_message, json_mode)

    async def _openai_chat(self, config: AIProviderConfig, system: str, user: str, json_mode: bool) -> str:
        AsyncOpenAI, _ = _openai_sdk()
        try:
            async with AsyncOpenAI(
                **_openai_client_kwargs(config, timeout=COMPLETION_TIMEOUT_SECONDS)
            ) as client:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
                if json_mode:
                    resp = await client.chat.completions.create(
                        model=config.model or "",
                        messages=messages,
                        temperature=0.1,
                        response_format={"type": "json_object"},
                    )
                else:
                    resp = await client.chat.completions.create(
                        model=config.model or "",
                        messages=messages,
                        temperature=0.1,
                    )
        except Exception as exc:
            raise AIProviderError(f"AI 调用失败：{exc}") from exc
        if not resp or not resp.choices:
            raise AIProviderError("AI 返回空结果")
        return resp.choices[0].message.content or ""

    async def _anthropic_chat(self, config: AIProviderConfig, system: str, user: str) -> str:
        if not config.api_key:
            raise AIProviderError("Anthropic Provider 需要 API Key。")
        system = system + "\n\nRespond with ONLY the JSON object, no markdown, no explanation."
        AsyncAnthropic, _ = _anthropic_sdk()
        try:
            async with AsyncAnthropic(
                **_anthropic_client_kwargs(config, timeout=COMPLETION_TIMEOUT_SECONDS)
            ) as client:
                resp = await client.messages.create(
                    model=config.model or "",
                    max_tokens=4096,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    temperature=0.1,
                )
        except Exception as exc:
            raise AIProviderError(f"Anthropic 调用失败：{exc}") from exc
        if not resp.content:
            return ""
        block = resp.content[0]
        if block.type == "text":
            return block.text
        return str(block)

    async def vision_completion(self, config: AIProviderConfig, base64_image: str) -> str:
        prompt = "Extract all text from this image. Return ONLY the text content, no extra commentary."
        media_type = _image_media_type(base64_image)
        if config.provider_type == "anthropic":
            return await self._anthropic_vision(config, base64_image, media_type, prompt)

        AsyncOpenAI, _ = _openai_sdk()
        try:
            async with AsyncOpenAI(
                **_openai_client_kwargs(config, timeout=COMPLETION_TIMEOUT_SECONDS)
            ) as client:
                resp = await client.chat.completions.create(
                    model=config.model or "",
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64_image}"}},
                    ]}],
                    max_tokens=2000,
                    temperature=0.1,
                )
            return resp.choices[0].message.content or ""
        except Exception as exc:
            raise AIProviderError(f"识图失败：{exc}") from exc

    async def _anthropic_vision(
        self,
        config: AIProviderConfig,
        base64_image: str,
        media_type: str,
        prompt: str,
    ) -> str:
        if not config.api_key:
            raise AIProviderError("Anthropic Provider 需要 API Key。")
        AsyncAnthropic, _ = _anthropic_sdk()
        try:
            async with AsyncAnthropic(
                **_anthropic_client_kwargs(config, timeout=COMPLETION_TIMEOUT_SECONDS)
            ) as client:
                resp = await client.messages.create(
                    model=config.model or "",
                    max_tokens=2000,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": base64_image,
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                    temperature=0.1,
                )
        except Exception as exc:
            raise AIProviderError(f"识图失败：{exc}") from exc
        if not resp.content:
            raise AIProviderError("识图模型返回空结果")
        text_blocks = [block.text for block in resp.content if getattr(block, "type", "") == "text"]
        return "\n".join(text_blocks)

    async def list_models(self, config: AIProviderConfig) -> list[str]:
        if config.provider_type == "anthropic":
            if not config.api_key:
                raise AIProviderError("Anthropic Provider 需要 API Key。")
            AsyncAnthropic, AnthropicAPIError = _anthropic_sdk()
            try:
                async with AsyncAnthropic(
                    **_anthropic_client_kwargs(config, timeout=PROBE_TIMEOUT_SECONDS)
                ) as client:
                    page = await client.models.list()
            except Exception as exc:
                if isinstance(exc, AnthropicAPIError):
                    raise AIProviderError(f"模型列表拉取失败：{_api_error_message(exc)}") from exc
                raise AIProviderError(f"模型列表拉取失败：{exc}") from exc
            model_ids = sorted({model.id for model in page.data if getattr(model, "id", None)})
            if not model_ids:
                raise AIProviderError("Provider 没有返回可用模型。")
            return model_ids

        AsyncOpenAI, OpenAIAPIError = _openai_sdk()
        try:
            async with AsyncOpenAI(
                **_openai_client_kwargs(config, timeout=PROBE_TIMEOUT_SECONDS)
            ) as client:
                page = await client.models.list()
        except Exception as exc:
            if isinstance(exc, OpenAIAPIError):
                raise AIProviderError(f"模型列表拉取失败：{_api_error_message(exc)}") from exc
            raise AIProviderError(f"模型列表拉取失败：{exc}") from exc

        model_ids = sorted({model.id for model in page.data if getattr(model, "id", None)})
        if not model_ids:
            raise AIProviderError("Provider 没有返回可用模型。")
        return model_ids

    async def test_connection(self, config: AIProviderConfig) -> None:
        if not config.model:
            raise AIProviderError("请先选择或输入模型。")

        if config.provider_type == "anthropic":
            await self._test_anthropic(config)
            return

        await self._test_openai_compatible(config)

    async def probe_schema_compliance(self, config: AIProviderConfig) -> dict[str, Any]:
        if not config.model:
            raise AIProviderError("请先选择或输入模型。")

        system_prompt = (
            "You are a calendar event extraction assistant. Extract event information from the user's message.\n"
            "Return ONLY a JSON object adhering to this schema:\n"
            "{\n"
            '  "intent": "create_event",\n'
            '  "events": [\n'
            "    {\n"
            '      "title": "...",\n'
            '      "start_time": "2026-10-15T15:00:00+08:00",\n'
            '      "is_all_day": false\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        user_message = "明天下午3点开会讨论项目进度"

        try:
            raw = await self.chat_completion(config, system_prompt, user_message, json_mode=True)
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(f"模型调用失败：{exc}") from exc

        if not raw or not raw.strip():
            raise AIProviderError("模型返回空结果，未能遵循 JSON Schema。")

        import json
        import re

        clean_text = raw.strip()
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", clean_text)
        if m:
            clean_text = m.group(1).strip()
        m_obj = re.search(r"\{[\s\S]*\}", clean_text)
        if m_obj:
            clean_text = m_obj.group(0)

        try:
            data = json.loads(clean_text)
        except Exception as exc:
            raise AIProviderError(f"模型未返回合法的 JSON 格式：{raw[:100]}") from exc

        if not isinstance(data, dict):
            raise AIProviderError("模型返回的 JSON 顶层必须为对象。")

        intent = data.get("intent")
        if not intent:
            raise AIProviderError("模型返回数据缺少 'intent' 字段。")

        events = data.get("events")
        if not events and "event" in data and isinstance(data["event"], dict):
            events = [data["event"]]

        if not isinstance(events, list) or not events:
            raise AIProviderError("模型返回数据缺少有效的 'events' 列表。")

        first_event = events[0]
        if not isinstance(first_event, dict):
            raise AIProviderError("日程事件项必须为对象。")

        title = first_event.get("title")
        start_time = first_event.get("start_time")

        if not title or not isinstance(title, str) or not title.strip():
            raise AIProviderError("模型未能提取到有效的日程标题 (title)。")
        if not start_time or not isinstance(start_time, str) or not start_time.strip():
            raise AIProviderError("模型未能提取到有效的日程时间 (start_time)。")

        return {
            "ok": True,
            "intent": str(intent),
            "title": title.strip(),
            "start_time": start_time.strip(),
            "raw": raw,
        }

    async def _test_openai_compatible(self, config: AIProviderConfig) -> None:
        AsyncOpenAI, _ = _openai_sdk()
        try:
            async with AsyncOpenAI(
                **_openai_client_kwargs(config, timeout=PROBE_TIMEOUT_SECONDS)
            ) as client:
                _ = await client.chat.completions.create(
                    model=config.model or "",
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=8,
                    temperature=0,
                )
        except Exception as exc:
            raise AIProviderError(safe_ai_connection_error(exc)) from exc

    async def _test_anthropic(self, config: AIProviderConfig) -> None:
        if not config.api_key:
            raise AIProviderError("Anthropic Provider 需要 API Key。")
        AsyncAnthropic, _ = _anthropic_sdk()
        try:
            async with AsyncAnthropic(
                **_anthropic_client_kwargs(config, timeout=PROBE_TIMEOUT_SECONDS)
            ) as client:
                _ = await client.messages.create(
                    model=config.model or "",
                    max_tokens=8,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    temperature=0,
                )
        except Exception as exc:
            raise AIProviderError(safe_ai_connection_error(exc)) from exc


class AIProviderError(Exception):
    pass


def _openai_sdk() -> tuple[Any, type[Exception]]:
    module = importlib.import_module("openai")
    return module.AsyncOpenAI, module.APIError


def _openai_client_kwargs(config: AIProviderConfig, *, timeout: float) -> dict[str, object]:
    # Some Cloudflare bot rules block the OpenAI Python SDK's default User-Agent
    # before a request reaches an OpenAI-compatible origin. Identify this app
    # explicitly so self-hosted gateways can distinguish it from the SDK default.
    return {
        "api_key": config.api_key or "local",
        "base_url": config.base_url,
        "default_headers": {"User-Agent": OPENAI_COMPATIBLE_USER_AGENT, **_opencode_headers(config)},
        "timeout": timeout,
        "max_retries": CLIENT_MAX_RETRIES,
    }


def _anthropic_sdk() -> tuple[Any, type[Exception]]:
    module = importlib.import_module("anthropic")
    return module.AsyncAnthropic, module.APIError


def _anthropic_client_kwargs(config: AIProviderConfig, *, timeout: float) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "api_key": config.api_key or "",
        "timeout": timeout,
        "max_retries": CLIENT_MAX_RETRIES,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if headers := _opencode_headers(config):
        kwargs["default_headers"] = headers
    return kwargs


def _image_media_type(base64_image: str) -> str:
    try:
        header = base64_module.b64decode(base64_image[:32], validate=False)
    except Exception:
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _api_error_message(exc: Exception) -> str:
    return str(getattr(exc, "message", exc))
