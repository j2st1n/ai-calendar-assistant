from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    provider_type: str
    base_url: str


PROVIDER_PRESETS = [
    ProviderPreset("OpenAI", "openai_compatible", "https://api.openai.com/v1"),
    ProviderPreset("DeepSeek", "openai_compatible", "https://api.deepseek.com/v1"),
    ProviderPreset("Moonshot / Kimi", "openai_compatible", "https://api.moonshot.cn/v1"),
    ProviderPreset("Qwen / DashScope", "openai_compatible", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ProviderPreset("OpenRouter", "openai_compatible", "https://openrouter.ai/api/v1"),
    ProviderPreset("SiliconFlow", "openai_compatible", "https://api.siliconflow.cn/v1"),
    ProviderPreset("Ollama", "openai_compatible", "http://127.0.0.1:11434/v1"),
    ProviderPreset("Custom", "openai_compatible", ""),
    ProviderPreset("Anthropic", "anthropic", "https://api.anthropic.com"),
]


CLAUDE_MODELS = [
    "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
]
