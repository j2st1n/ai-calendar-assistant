from dataclasses import dataclass

from anthropic import APIError as AnthropicAPIError
from anthropic import AsyncAnthropic
from openai import APIError as OpenAIAPIError
from openai import AsyncOpenAI

from app.ai.providers import CLAUDE_MODELS


@dataclass(frozen=True)
class AIProviderConfig:
    provider_type: str
    base_url: str
    api_key: str | None
    model: str | None = None


class AIProviderService:
    async def list_models(self, config: AIProviderConfig) -> list[str]:
        if config.provider_type == "anthropic":
            return CLAUDE_MODELS

        client = AsyncOpenAI(
            api_key=config.api_key or "local",
            base_url=config.base_url,
        )
        try:
            page = await client.models.list()
        except OpenAIAPIError as exc:
            raise AIProviderError(f"模型列表拉取失败：{exc.message}") from exc
        except Exception as exc:
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
        client = AsyncOpenAI(
            api_key=config.api_key or "local",
            base_url=config.base_url,
        )
        try:
            await client.chat.completions.create(
                model=config.model or "",
                messages=[{"role": "user", "content": "Reply with OK."}],
                max_tokens=8,
                temperature=0,
            )
        except OpenAIAPIError as exc:
            raise AIProviderError(f"连接测试失败：{exc.message}") from exc
        except Exception as exc:
            raise AIProviderError(f"连接测试失败：{exc}") from exc

    async def _test_anthropic(self, config: AIProviderConfig) -> None:
        if not config.api_key:
            raise AIProviderError("Anthropic Provider 需要 API Key。")
        client = AsyncAnthropic(api_key=config.api_key, base_url=config.base_url or None)
        try:
            await client.messages.create(
                model=config.model or "",
                max_tokens=8,
                messages=[{"role": "user", "content": "Reply with OK."}],
                temperature=0,
            )
        except AnthropicAPIError as exc:
            raise AIProviderError(f"连接测试失败：{exc.message}") from exc
        except Exception as exc:
            raise AIProviderError(f"连接测试失败：{exc}") from exc


class AIProviderError(Exception):
    pass
