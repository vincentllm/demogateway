from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


EnvPattern = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def _resolve_env(value: Any) -> Any:
    """Recursively resolve ${VAR} or ${VAR:default} in YAML-loaded values."""
    if isinstance(value, str):

        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default = match.group(2)
            return os.environ.get(var_name, default if default is not None else match.group(0))

        return EnvPattern.sub(replacer, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class RedisConfig(BaseModel):
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    key_prefix: str = "ai_gateway:rl"


class StorageConfig(BaseModel):
    type: Literal["memory", "redis"] = "memory"
    redis: RedisConfig = Field(default_factory=RedisConfig)


class RateLimitPolicyConfig(BaseModel):
    name: str
    enabled: bool = True
    algorithm: Literal["fixed_window"] = "fixed_window"
    granularity: Literal["request", "token"] = "request"
    window_seconds: int = Field(default=60, ge=1)
    limit: int = Field(default=100, ge=1)


class RateLimiterConfig(BaseModel):
    policies: list[RateLimitPolicyConfig] = Field(default_factory=list)
    default_key_source: Literal["api_key", "ip"] = "api_key"
    storage: StorageConfig = Field(default_factory=StorageConfig)


class TokenizerConfig(BaseModel):
    type: Literal["dummy"] = "dummy"
    tokens_per_message: int = 10
    overhead: int = 5


class BackendConfig(BaseModel):
    id: str
    provider: Literal["openai", "zai"] = "openai"
    base_url: str
    api_key: str | None = None
    weight: int = Field(default=1, ge=1)
    models: list[str] = Field(default_factory=list)
    health_url: str | None = None


class FallbackConfig(BaseModel):
    enabled: bool = True
    message: str = "Service temporarily unavailable, please retry later."
    model_downgrade: dict[str, str] = Field(default_factory=dict)


class ResilienceConfig(BaseModel):
    max_retries: int = Field(default=3, ge=0)
    retry_status_codes: list[int] = Field(default_factory=lambda: [429, 502, 503, 504])
    timeout: float = Field(default=30.0, ge=0.1)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


class HealthCheckConfig(BaseModel):
    interval_seconds: float = Field(default=10.0, ge=1.0)
    timeout_seconds: float = Field(default=3.0, ge=0.1)
    unhealthy_threshold: int = Field(default=2, ge=1)


class LoadMetricsConfig(BaseModel):
    latency_threshold_ms: float = Field(default=2000.0, ge=0.0)
    error_rate_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    window_size: int = Field(default=20, ge=1)


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    rate_limiter: RateLimiterConfig = Field(default_factory=RateLimiterConfig)
    tokenizer: TokenizerConfig = Field(default_factory=TokenizerConfig)
    backends: list[BackendConfig] = Field(default_factory=list)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    load_metrics: LoadMetricsConfig = Field(default_factory=LoadMetricsConfig)


CONFIG_PATHS = [
    Path("config.yaml"),
    Path("/etc/ai-gateway/config.yaml"),
]


def load_config(path: Path | str | None = None) -> GatewayConfig:
    target: Path | None = Path(path) if path else None
    if target is None:
        env_path = os.environ.get("GATEWAY_CONFIG")
        if env_path:
            target = Path(env_path)
    if target is None:
        for candidate in CONFIG_PATHS:
            if candidate.exists():
                target = candidate
                break
    if target is None or not target.exists():
        raise FileNotFoundError(f"Config file not found: looked for {CONFIG_PATHS}")

    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    resolved = _resolve_env(raw)
    return GatewayConfig.model_validate(resolved)
