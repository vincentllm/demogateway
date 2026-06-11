from ai_gateway.providers.base import Provider, ProviderRegistry
from ai_gateway.providers.openai_provider import OpenAIProvider
from ai_gateway.providers.zai_provider import ZaiProvider

BUILTIN_PROVIDERS: list[Provider] = [
    OpenAIProvider(),
    ZaiProvider(),
]

__all__ = [
    "Provider",
    "ProviderRegistry",
    "OpenAIProvider",
    "ZaiProvider",
    "BUILTIN_PROVIDERS",
]
