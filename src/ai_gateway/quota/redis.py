from __future__ import annotations

import hashlib
import json
from typing import Any

from .core import QuotaUnavailable
from .redis_script import SCRIPT


def _wire(value: Any) -> Any:
    # Redis cjson emits only 14 significant digits. Decimal strings preserve all
    # validated integers through JSON and Lua's exactly representable range.
    if type(value) is int:
        return str(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    return value


class RedisQuotaStore:
    """One EVAL and one mutating HSET per operation with an injected client.

    No dependency import or connection lifecycle is owned here. A client account
    and its leases occupy one hash key. Redis TIME, not worker clocks, defines windows.
    Durable state MUST NOT expire or be evicted; see README operational contract.
    """

    def __init__(self, redis_client: Any, key_prefix: str = "ai_gateway:quota:v1"):
        if not isinstance(key_prefix, str) or not key_prefix or any(
            c in key_prefix for c in "{}\r\n"
        ):
            raise ValueError("key_prefix must be nonempty and contain no braces or newlines")
        self._redis = redis_client
        self._prefix = key_prefix
        self._script = SCRIPT

    async def execute(self, operation: str, client_id: str, request_id: str, data: dict) -> dict:
        client_hash = hashlib.sha256(client_id.encode()).hexdigest()
        request_hash = hashlib.sha256(request_id.encode()).hexdigest()
        base = f"{self._prefix}:{{{client_hash}}}"
        try:
            raw = await self._redis.eval(
                self._script, 1, base + ":account",
                operation, client_id, request_id, json.dumps(_wire(data), separators=(",", ":")),
                request_hash,
            )
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid store result")
            if operation == "admit":
                if "reason" not in result and not isinstance(result.get("lease"), dict):
                    raise ValueError("invalid admission result")
                if "lease" in result:
                    for name in ("input_bound", "max_output_tokens", "reserved_tokens", "window"):
                        result["lease"][name] = int(result["lease"][name])
            elif "reason" not in result and type(result.get("changed")) is not bool:
                raise ValueError("invalid terminal result")
            return result
        except Exception:
            # Even a timeout may follow an applied script: caller must never dispatch
            # upstream on this path. A retry uses the SAME request id, never fallback.
            raise QuotaUnavailable() from None
