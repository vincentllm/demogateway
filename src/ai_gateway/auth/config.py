"""Immutable configuration snapshots; no filesystem or environment side effects.

Client keys must be randomly generated, high-entropy secrets. Only SHA-256
digests belong in client configuration. These digests are not password hashes.
Upstream keys are deliberately separate and redacted by SecretStr.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class AuthConfigurationError(ValueError):
    """Invalid configuration, with a message that never includes input values."""


def _name(value: Any) -> None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise AuthConfigurationError("Invalid authentication identifier")


def _enabled(value: Any) -> None:
    if not isinstance(value, bool):
        raise AuthConfigurationError("Enabled must be a boolean")


def _token(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 4096 and bool(_TOKEN.fullmatch(value))


def hash_client_key(token: str) -> str:
    """Return a lowercase SHA-256 digest of a high-entropy raw client key.

    The input is a raw token, never an Authorization header. Invalid inputs
    raise a redacted configuration error, without echoing the token.
    """
    if not _token(token):
        raise AuthConfigurationError("Invalid API key format")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _tuple_of(value: Any, item_type: type) -> tuple:
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise AuthConfigurationError("Invalid authentication configuration collection")
    return tuple(value)


def _models(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)) or any(
        not isinstance(model, str) or not model or len(model) > 256
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model)
        for model in value
    ):
        raise AuthConfigurationError("Invalid allowed model list")
    return frozenset(value)


@dataclass(frozen=True, kw_only=True)
class ClientKeyConfig:
    key_id: str
    sha256: str = field(repr=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        _name(self.key_id)
        _enabled(self.enabled)
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise AuthConfigurationError("Client key must be a lowercase SHA-256 digest")


@dataclass(frozen=True, kw_only=True)
class ClientConfig:
    client_id: str
    keys: tuple[ClientKeyConfig, ...] = ()
    allowed_models: frozenset[str] = frozenset()
    enabled: bool = True

    def __post_init__(self) -> None:
        _name(self.client_id)
        _enabled(self.enabled)
        object.__setattr__(self, "keys", _tuple_of(self.keys, ClientKeyConfig))
        object.__setattr__(self, "allowed_models", _models(self.allowed_models))


@dataclass(frozen=True, kw_only=True)
class UpstreamKeyConfig:
    key_id: str
    secret: SecretStr | str = field(repr=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        _name(self.key_id)
        _enabled(self.enabled)
        value = self.secret.get_secret_value() if isinstance(self.secret, SecretStr) else self.secret
        if not _token(value):
            raise AuthConfigurationError("Invalid upstream API key format")
        object.__setattr__(self, "secret", SecretStr(value))


@dataclass(frozen=True, kw_only=True)
class UpstreamPoolConfig:
    key_ref: str
    keys: tuple[UpstreamKeyConfig, ...] = ()

    def __post_init__(self) -> None:
        _name(self.key_ref)
        object.__setattr__(self, "keys", _tuple_of(self.keys, UpstreamKeyConfig))
        ids = [key.key_id for key in self.keys]
        if len(ids) != len(set(ids)):
            raise AuthConfigurationError("Duplicate upstream key identifier")


@dataclass(frozen=True, kw_only=True)
class AuthConfig:
    clients: tuple[ClientConfig, ...] = ()
    upstream_pools: tuple[UpstreamPoolConfig, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "clients", _tuple_of(self.clients, ClientConfig))
        object.__setattr__(self, "upstream_pools", _tuple_of(self.upstream_pools, UpstreamPoolConfig))
        client_ids = [client.client_id for client in self.clients]
        refs = [pool.key_ref for pool in self.upstream_pools]
        keys = [key for client in self.clients for key in client.keys]
        key_ids = [key.key_id for key in keys]
        digests = [key.sha256 for key in keys]
        if len(client_ids) != len(set(client_ids)) or len(refs) != len(set(refs)):
            raise AuthConfigurationError("Duplicate client or upstream pool identifier")
        if len(key_ids) != len(set(key_ids)) or len(digests) != len(set(digests)):
            raise AuthConfigurationError("Duplicate client key identifier or digest")
        # Even disabled credentials cannot silently acquire a second identity
        # or be repurposed as a provider credential during a rotation.
        client_digests = set(digests)
        for pool in self.upstream_pools:
            for key in pool.keys:
                if hash_client_key(key.secret.get_secret_value()) in client_digests:
                    raise AuthConfigurationError("Client and upstream keys must be separate")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AuthConfig:
        """Parse the auth section of an already-loaded YAML/JSON mapping.

        Unknown fields and coercions such as enabled="false" are rejected.
        This parser never reads files, expands environment variables, or logs
        source values. The integration owner supplies resolved upstream secrets.
        """
        def construct(kind: type, item: Any, nested: dict | None = None):
            if not isinstance(item, Mapping):
                raise AuthConfigurationError("Invalid authentication configuration mapping")
            fields = dict(item)
            for name, converter in (nested or {}).items():
                if name in fields:
                    raw = fields[name]
                    if not isinstance(raw, (tuple, list)):
                        raise AuthConfigurationError("Invalid authentication configuration collection")
                    fields[name] = tuple(converter(entry) for entry in raw)
            try:
                return kind(**fields)
            except TypeError:
                raise AuthConfigurationError("Unknown or missing authentication configuration field") from None

        client = lambda item: construct(ClientConfig, item, {"keys": lambda key: construct(ClientKeyConfig, key)})
        pool = lambda item: construct(UpstreamPoolConfig, item, {"keys": lambda key: construct(UpstreamKeyConfig, key)})
        return construct(cls, value, {"clients": client, "upstream_pools": pool})
