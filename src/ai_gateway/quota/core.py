from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Mapping, Protocol
from uuid import uuid4

# Redis Lua uses IEEE-754 numbers; keep all public amounts exactly representable.
MAX_VALUE = 2**53 - 1


def _integer(value: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= MAX_VALUE:
        raise ValueError(f"{name} must be an integer between 0 and {MAX_VALUE}")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        raise ValueError("identifiers must contain 1 to 1024 UTF-8 bytes")


@dataclass(frozen=True)
class QuotaLimits:
    rpm: int
    tpm: int
    total_tokens: int
    window_seconds: int = 60

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _integer(value, name)
        if not 1 <= self.window_seconds <= 86400:
            raise ValueError("window_seconds must be between 1 and 86400")


@dataclass(frozen=True)
class Lease:
    client_id: str
    request_id: str
    lease_id: str
    input_bound: int
    max_output_tokens: int
    reserved_tokens: int
    window: int


class ReleaseReason(str, Enum):
    NOT_SENT = "not_sent"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UPSTREAM_ERROR = "upstream_error"
    UNKNOWN = "unknown"


class QuotaRejected(Exception):
    """Admission refusal. None retry_after means no timed retry is promised."""

    def __init__(self, reason: str, retry_after: int | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


class QuotaUnavailable(QuotaRejected):
    def __init__(self) -> None:
        super().__init__("storage_unavailable", 1)


class QuotaStore(Protocol):
    async def execute(self, operation: str, client_id: str, request_id: str,
                      data: dict) -> dict: ...


class QuotaManager:
    """Async facade. Config is a snapshot; stores must be shared by managers.

    input_bound MUST be a trusted upper bound supplied by the integration layer,
    never a dummy tokenizer estimate. Duplicate request IDs are rejected even
    after terminalization to prevent an idempotent reservation funding two calls.
    """

    def __init__(self, limits: Mapping[str, QuotaLimits], store: QuotaStore | None = None):
        for client_id, value in limits.items():
            _identifier(client_id)
            if not isinstance(value, QuotaLimits):
                raise ValueError("limits must contain QuotaLimits values")
        self._limits = dict(limits)
        self._store = store if store is not None else MemoryQuotaStore()

    async def admit(self, client_id: str, request_id: str, input_bound: int,
                    max_output_tokens: int) -> Lease:
        _identifier(client_id)
        _identifier(request_id)
        _integer(input_bound, "input_bound")
        _integer(max_output_tokens, "max_output_tokens")
        reserved = input_bound + max_output_tokens
        _integer(reserved, "reserved_tokens")
        limits = self._limits.get(client_id)
        if limits is None:
            raise QuotaRejected("unknown_client")
        result = await self._store.execute("admit", client_id, request_id, {
            "limits": asdict(limits), "input_bound": input_bound,
            "max_output_tokens": max_output_tokens, "reserved_tokens": reserved,
            "lease_id": uuid4().hex,
        })
        if "reason" in result:
            raise QuotaRejected(result["reason"], result.get("retry_after"))
        return Lease(**result["lease"])

    async def settle(self, lease: Lease, usage: Mapping[str, int] | None) -> bool:
        """Charge actual usage, or keep the entire reservation if unknown.

        Returns True only for the first terminal transition. Valid usage exceeding
        the reservation creates debt and blocks later admission; it is not clipped.
        """
        total = None
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise ValueError("usage must be a mapping or None")
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                _integer(usage.get(name), name)
            total = usage["total_tokens"]
            if total != usage["prompt_tokens"] + usage["completion_tokens"]:
                raise ValueError("usage total must equal prompt plus completion")
        return await self._terminal(lease, total, "settled" if usage is not None else "retained")

    async def release(self, lease: Lease, reason: ReleaseReason | str) -> bool:
        """Only NOT_SENT certifies no upstream dispatch and permits a refund.

        Timeout/cancel/error alone cannot establish zero usage. All other reasons
        retain the full reservation. RPM is never refunded.
        """
        reason = ReleaseReason(reason)
        refund = reason is ReleaseReason.NOT_SENT
        return await self._terminal(lease, 0 if refund else None,
                                    "released" if refund else "retained")

    async def _terminal(self, lease: Lease, total: int | None, state: str) -> bool:
        if not isinstance(lease, Lease):
            raise ValueError("expected Lease")
        _identifier(lease.client_id)
        _identifier(lease.request_id)
        _identifier(lease.lease_id)
        for name in ("input_bound", "max_output_tokens", "reserved_tokens", "window"):
            _integer(getattr(lease, name), name)
        if lease.reserved_tokens != lease.input_bound + lease.max_output_tokens:
            raise ValueError("mismatched lease reservation")
        result = await self._store.execute("terminal", lease.client_id, lease.request_id,
                                           {"lease": asdict(lease), "total": total, "state": state})
        if result.get("reason") == "invalid_lease":
            raise ValueError("unknown or mismatched lease")
        return result["changed"]


class MemoryQuotaStore:
    """Atomic within ONE process/store object, including threads/event loops.

    Fixed windows charge tokens to the admission window. Delayed settlements do
    not subtract from a newer window. Pending reservations never auto-refund.
    Lease tombstones persist for request-id deduplication; see README for sizing.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}
        self._leases: dict[tuple[str, str], dict] = {}

    async def execute(self, operation: str, client_id: str, request_id: str, data: dict) -> dict:
        with self._lock:
            key = (client_id, request_id)
            record = self._leases.get(key)
            if operation == "admit":
                if record is not None:
                    return {"reason": "duplicate_request"}
                limits = data["limits"]
                now = self._clock()
                if not math.isfinite(now) or now < 0:
                    raise QuotaUnavailable()
                window = int(now // limits["window_seconds"])
                account = self._clients.get(client_id)
                if account is not None and account["policy"] != limits:
                    return {"reason": "policy_mismatch"}
                if account is not None and account.get("overflow"):
                    return {"reason": "account_overflow"}
                account = account or {"window": window, "rpm": 0, "tokens": 0,
                                      "total": 0, "policy": dict(limits)}
                # Never roll a window backward after wall-clock correction.
                window = max(window, account["window"])
                rpm = account["rpm"] if account["window"] == window else 0
                tokens = account["tokens"] if account["window"] == window else 0
                amount = data["reserved_tokens"]
                retry = max(1, math.ceil((window + 1) * limits["window_seconds"] - now))
                if amount > limits["tpm"]:
                    return {"reason": "reservation_too_large"}
                if account["total"] + amount > limits["total_tokens"]:
                    return {"reason": "total_tokens"}
                if rpm >= limits["rpm"]:
                    return {"reason": "rpm", "retry_after": retry}
                if tokens + amount > limits["tpm"]:
                    return {"reason": "tpm", "retry_after": retry}
                lease = {name: data[name] for name in (
                    "lease_id", "input_bound", "max_output_tokens", "reserved_tokens")}
                lease.update(client_id=client_id, request_id=request_id, window=window)
                self._clients[client_id] = {"window": window, "rpm": rpm + 1,
                    "tokens": tokens + amount, "total": account["total"] + amount,
                    "policy": dict(limits)}
                self._leases[key] = {"lease": lease, "state": "pending"}
                return {"lease": dict(lease)}
            if record is None or record["lease"] != data["lease"]:
                return {"reason": "invalid_lease"}
            if record["state"] != "pending":
                return {"changed": False}
            lease = record["lease"]
            amount = data["total"] if data["total"] is not None else lease["reserved_tokens"]
            delta = amount - lease["reserved_tokens"]
            account = self._clients[client_id]
            # Overflow latches admission closed, even if another lease later refunds.
            if account["total"] + delta > MAX_VALUE or (
                account["window"] == lease["window"] and account["tokens"] + delta > MAX_VALUE
            ):
                account["overflow"] = True
            account["total"] = min(MAX_VALUE, account["total"] + delta)
            if account["window"] == lease["window"]:
                account["tokens"] = min(MAX_VALUE, account["tokens"] + delta)
            record["state"] = data["state"]
            return {"changed": True}
