from dataclasses import dataclass
import base64 as base64_module
import importlib
from typing import Any

OPENAI_COMPATIBLE_USER_AGENT = "ai-calendar-assistant"
PROBE_TIMEOUT_SECONDS = 20.0
COMPLETION_TIMEOUT_SECONDS = 60.0
CLIENT_MAX_RETRIES = 1


@dataclass(frozen=True)
class AIProviderConfig:
    provider_type: str
    base_url: str
    api_key: str | None
    model: str | None = None


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

    async def _test_openai_compatible(self, config: AIProviderConfig) -> None:
        AsyncOpenAI, OpenAIAPIError = _openai_sdk()
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
            if isinstance(exc, OpenAIAPIError):
                raise AIProviderError(f"连接测试失败：{_api_error_message(exc)}") from exc
            raise AIProviderError(f"连接测试失败：{exc}") from exc

    async def _test_anthropic(self, config: AIProviderConfig) -> None:
        if not config.api_key:
            raise AIProviderError("Anthropic Provider 需要 API Key。")
        AsyncAnthropic, AnthropicAPIError = _anthropic_sdk()
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
            if isinstance(exc, AnthropicAPIError):
                raise AIProviderError(f"连接测试失败：{_api_error_message(exc)}") from exc
            raise AIProviderError(f"连接测试失败：{exc}") from exc


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
        "default_headers": {"User-Agent": OPENAI_COMPATIBLE_USER_AGENT},
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
