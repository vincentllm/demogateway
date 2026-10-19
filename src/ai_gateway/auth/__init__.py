"""Explicit, configuration-driven gateway authentication and upstream key pools."""

from .config import (
    AuthConfig,
    AuthConfigurationError,
    ClientConfig,
    ClientKeyConfig,
    UpstreamKeyConfig,
    UpstreamPoolConfig,
    hash_client_key,
)
from .manager import AuthError, AuthManager, Identity, UpstreamKeyUnavailable

__all__ = [
    "AuthConfig", "AuthConfigurationError", "AuthError", "AuthManager",
    "ClientConfig", "ClientKeyConfig", "Identity", "UpstreamKeyConfig",
    "UpstreamKeyUnavailable", "UpstreamPoolConfig", "hash_client_key",
]
