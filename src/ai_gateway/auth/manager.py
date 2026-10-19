"""Authentication and named upstream pools, independent of app state and routing."""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from .config import AuthConfig, AuthConfigurationError, ClientConfig, ClientKeyConfig, _models, _name, _token


class AuthError(Exception):
    """A safe OpenAI-style client authentication/authorization error."""

    def __init__(self, *, forbidden: bool = False) -> None:
        self.status_code = 403 if forbidden else 401
        self.code = "model_not_allowed" if forbidden else "invalid_api_key"
        self.type = "permission_error" if forbidden else "authentication_error"
        self.message = "Model access is not permitted." if forbidden else "Invalid API key."
        super().__init__(self.message)

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"error": {"message": self.message, "type": self.type, "param": None, "code": self.code}},
            headers={"WWW-Authenticate": "Bearer"} if self.status_code == 401 else None,
        )


class UpstreamKeyUnavailable(Exception):
    """A server-side credential/configuration failure, never a client 401."""

    status_code = 503

    def __init__(self) -> None:
        super().__init__("No enabled upstream API key is configured.")

    def to_response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content={"error": {
            "message": str(self), "type": "server_error", "param": None,
            "code": "upstream_key_unavailable",
        }})


@dataclass(frozen=True, kw_only=True)
class Identity:
    client_id: str
    allowed_models: frozenset[str]

    def __post_init__(self) -> None:
        _name(self.client_id)
        object.__setattr__(self, "allowed_models", _models(self.allowed_models))


class AuthManager:
    """O(1) digest lookup with immutable snapshots and atomic configuration reload.

    ``authenticate`` accepts a COMPLETE Authorization header, e.g. "Bearer sk-...".
    ``hash_client_key`` instead accepts the raw token. No header is implicitly
    reused for upstream requests. Only ``resolve_upstream_key`` reveals a provider
    secret, selected round-robin from enabled keys in a server-selected pool.

    Call reload after loading a new validated configuration. A failed reload
    leaves the previous snapshot active. Revocation applies to calls beginning
    after reload returns; already-authorized in-flight work is not cancelled.
    All operations are synchronous, contain no network I/O and are thread-safe.
    """

    def __init__(self, config: AuthConfig) -> None:
        self._lock = threading.Lock()
        self.reload(config)

    def __repr__(self) -> str:
        return "AuthManager(<redacted>)"

    def reload(self, config: AuthConfig) -> None:
        if not isinstance(config, AuthConfig):
            raise AuthConfigurationError("AuthManager requires an AuthConfig snapshot")
        clients: dict[str, tuple[ClientConfig, ClientKeyConfig]] = {
            key.sha256: (client, key)
            for client in config.clients for key in client.keys
        }
        pools = {pool.key_ref: tuple(key for key in pool.keys if key.enabled)
                 for pool in config.upstream_pools}
        with self._lock:
            self._clients = clients
            self._pools = pools
            self._cursors = {key_ref: 0 for key_ref in pools}

    def authenticate(self, bearer: str | None, model: str) -> Identity:
        # Exactly one header value, one scheme and one token. Allow RFC bearer
        # scheme casing and SP separators, but no tabs, commas, CRLF or OWS.
        match = re.fullmatch(r"(?i:Bearer) +([^ ]+)", bearer) if isinstance(bearer, str) and len(bearer) <= 4110 else None
        token = match.group(1) if match else None
        if not _token(token):
            raise AuthError()
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._lock:
            found = self._clients.get(digest)
            expected = found[1].sha256 if found else "0" * 64
            matches = hmac.compare_digest(digest, expected)
            if not found or not matches or not found[0].enabled or not found[1].enabled:
                raise AuthError()
            client = found[0]
            if not isinstance(model, str) or not model or len(model) > 256 or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model
            ) or ("*" not in client.allowed_models and model not in client.allowed_models):
                raise AuthError(forbidden=True)
            return Identity(client_id=client.client_id, allowed_models=client.allowed_models)

    def resolve_upstream_key(self, key_ref: str) -> str:
        if not isinstance(key_ref, str):
            raise UpstreamKeyUnavailable()
        with self._lock:
            keys = self._pools.get(key_ref, ())
            if not keys:
                raise UpstreamKeyUnavailable()
            index = self._cursors[key_ref]
            self._cursors[key_ref] = (index + 1) % len(keys)
            return keys[index].secret.get_secret_value()
