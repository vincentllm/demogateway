# RFD-007: Provider Abstraction and Auto-Registration

| Field | Value |
|-------|-------|
| RFD ID | 007 |
| Title | Provider Abstraction and Auto-Registration |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Introduce a `Provider` abstraction that isolates protocol differences between LLM services, and auto-register all built-in providers via a single `BUILTIN_PROVIDERS` list. The first implementation targets OpenAI-compatible backends; a `ZaiProvider` placeholder is included to demonstrate how new providers are added.

## Context

The gateway must support OpenAI-format requests today and may need to support additional providers (e.g., zai) later. Hardcoding provider selection into the routing layer would make future extensions difficult and error-prone.

## Goals

- Expose a single OpenAI-compatible endpoint to clients.
- Support multiple backend providers without changing core routing.
- Make adding a new provider a matter of implementing an interface and adding it to a list.
- Route to the correct provider automatically based on route and/or backend configuration.

## Non-Goals

- Converting every provider's response into a canonical internal model in v0.1.
- Supporting non-HTTP LLM backends.
- Dynamic provider loading from external packages.

## Options Considered

### Option A: Provider Interface + Hardcoded Registry

Define a `Provider` interface and manually instantiate each provider in `main.py`:

```python
providers = ProviderRegistry([OpenAIProvider(), ZaiProvider()])
```

**Pros**

- Clean separation between gateway core and provider-specific logic.
- New providers can be registered without touching the router.

**Cons**

- Adding a provider still requires editing `main.py`.
- Easy to forget to register a new provider.

### Option B: Provider Interface + Auto-Registered `BUILTIN_PROVIDERS` List (Chosen)

Define a `Provider` interface and maintain a `BUILTIN_PROVIDERS` list in `ai_gateway.providers`:

```python
# ai_gateway/providers/__init__.py
BUILTIN_PROVIDERS: list[Provider] = [
    OpenAIProvider(),
    ZaiProvider(),
]
```

`main.py` simply does:

```python
from ai_gateway.providers import BUILTIN_PROVIDERS
providers = ProviderRegistry(BUILTIN_PROVIDERS)
```

**Pros**

- Adding a provider only requires creating a class and appending it to `BUILTIN_PROVIDERS`.
- No changes to `main.py` or the router.
- The registry is still explicit and easy to inspect.

**Cons**

- Slightly more indirection than Option A.
- Providers must be importable at startup.

### Option C: Plugin Discovery via Entry Points

Use Python entry points or filesystem scanning to discover providers dynamically.

**Pros**

- Third-party providers can be installed as separate packages.

**Cons**

- Adds complexity.
- Overkill for the first version where all providers live in the same repo.

### Option D: Hardcoded OpenAI Logic in Router

**Pros**

- Fewest abstractions.

**Cons**

- Adding zai or another provider requires modifying the router.
- Violates the requirement to plan for future provider expansion.

## Decision

Use **Option B**: a `Provider` interface with auto-registration via `BUILTIN_PROVIDERS`.

The router resolves the provider by route using `ProviderRegistry.resolve(route, body)`. Each provider decides whether it supports a given route and performs request/response transformation.

## How to Add a New Provider

1. Create `src/ai_gateway/providers/my_provider.py`:

```python
from ai_gateway.providers.base import Provider

class MyProvider(Provider):
    @property
    def name(self) -> str:
        return "my_provider"

    def supports(self, route: str, body: dict | None = None) -> bool:
        return route == "/v1/chat/completions"

    def transform_request(self, backend, route, body):
        url = backend.config.base_url.rstrip("/") + route
        headers = {}
        if backend.config.api_key:
            headers["authorization"] = f"Bearer {backend.config.api_key}"
        # Convert body to provider-specific format if needed.
        return url, body, headers

    def transform_response(self, raw, request_body):
        # Convert provider response to OpenAI-compatible format if needed.
        ...
```

2. Register it in `src/ai_gateway/providers/__init__.py`:

```python
from ai_gateway.providers.my_provider import MyProvider

BUILTIN_PROVIDERS: list[Provider] = [
    OpenAIProvider(),
    ZaiProvider(),
    MyProvider(),
]
```

3. Use it in `config.yaml`:

```yaml
backends:
  - id: my-backend
    provider: my_provider
    base_url: "https://api.my-provider.example.com"
```

No changes to `main.py` or the router are required.

## Consequences

### Positive

- Adding a provider is a localized change.
- The registry is explicit and version-controlled.
- Core routing logic remains provider-agnostic.

### Negative

- Provider authors must understand both the upstream protocol and OpenAI streaming semantics.
- The gateway cannot easily modify response content in v0.1.

## Limitations

- Only `OpenAIProvider` is fully implemented; `ZaiProvider` is a placeholder.
- Response transformation is limited to header/body passthrough in v0.1.
- Provider selection is route-based; per-model provider selection is future work.

## Future Work

- Implement `ZaiProvider` with proper request/response mapping.
- Add a response middleware layer for logging, filtering, or format normalization.
- Support provider-specific retry and timeout rules.
- Allow provider selection based on model name or backend configuration.
