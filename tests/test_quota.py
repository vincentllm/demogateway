"""Behavioral quota tests; real Redis is opt-in via GW_QUOTA_REDIS_SOCKET."""
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from ai_gateway.quota import (
    MemoryQuotaStore, QuotaLimits, QuotaManager, QuotaRejected,
    QuotaUnavailable, RedisQuotaStore, ReleaseReason,
)
from ai_gateway.quota.core import MAX_VALUE


class RedisWire:
    """Minimal real RESP client for EVAL: no optional Python dependency required."""

    def __init__(self, socket, username=None):
        self.socket = socket
        self.username = username

    async def eval(self, *args):
        return await self.command("EVAL", *args)

    async def command(self, *args):
        reader, writer = await asyncio.open_unix_connection(self.socket)
        try:
            async def exchange(values):
                parts = [str(value).encode() for value in values]
                writer.write(b"*%d\r\n" % len(parts) + b"".join(
                    b"$%d\r\n" % len(value) + value + b"\r\n" for value in parts))
                await writer.drain()
                line = await reader.readline()
                if line[:1] == b"-":
                    raise RuntimeError(line.decode())
                if line[:1] == b"+":
                    return line[1:-2]
                if line[:1] == b":":
                    return int(line[1:])
                assert line[:1] == b"$", line
                size = int(line[1:])
                payload = await reader.readexactly(size + 2)
                return payload[:-2]
            if self.username:
                assert await exchange(("AUTH", self.username, "")) == b"OK"
            return await exchange(args)
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture(params=["memory", "redis"])
def stores(request):
    if request.param == "memory":
        store = MemoryQuotaStore()
        return store, store
    socket = os.environ.get("GW_QUOTA_REDIS_SOCKET")
    if not socket:
        pytest.skip("real Redis not configured (GW_QUOTA_REDIS_SOCKET)")
    prefix = "gw02:test:" + uuid4().hex
    # Different clients/stores, shared Redis namespace; no flush of others' data.
    return (RedisQuotaStore(RedisWire(socket), prefix),
            RedisQuotaStore(RedisWire(socket), prefix))


def manager(store=None, rpm=1000, tpm=1000, total=1000):
    return QuotaManager({"a": QuotaLimits(rpm, tpm, total),
                         "b": QuotaLimits(rpm, tpm, total)}, store)


async def refused(awaitable, reason, retry=None):
    with pytest.raises(QuotaRejected) as caught:
        await awaitable
    assert caught.value.reason == reason
    if retry is not None:
        assert caught.value.retry_after == retry
    return caught.value


async def test_concurrent_atomic_admission_and_client_isolation(stores):
    managers = [manager(s, total=20) for s in stores]
    async def attempt(i):
        try:
            return await managers[i % 2].admit("a", str(i), 1, 2)
        except QuotaRejected as error:
            assert error.reason == "total_tokens"
            return None
    results = await asyncio.gather(*(attempt(i) for i in range(100)))
    assert sum(value is not None for value in results) == 6
    assert (await managers[0].admit("a", "last", 2, 0)).reserved_tokens == 2
    await refused(managers[1].admit("a", "overflow", 1, 0), "total_tokens")
    assert (await managers[1].admit("b", "last", 10, 10)).client_id == "b"


@pytest.mark.parametrize("kind", ["rpm", "tpm"])
async def test_rate_rejection_is_atomic_and_reports_retry(stores, kind):
    store = stores[0]
    q = manager(store, rpm=1 if kind == "rpm" else 100, tpm=4, total=100)
    lease = await q.admit("a", "one", 2, 2)
    error = await refused(q.admit("a", "two", 1, 0), kind)
    assert 1 <= error.retry_after <= 60
    await q.release(lease, "not_sent")
    if kind == "rpm":
        await refused(q.admit("a", "three", 0, 0), "rpm")
    else:
        # Rejected attempts never consumed RPM, total, or a request-id tombstone.
        await q.admit("a", "two", 4, 0)


async def test_settle_refund_and_terminal_idempotency(stores):
    q, other = (manager(s, total=10) for s in stores)
    lease = await q.admit("a", "one", 5, 5)
    with pytest.raises(FrozenInstanceError):
        lease.window = 2
    assert await other.settle(lease, {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4})
    assert not await q.settle(lease, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    assert not await q.release(lease, "not_sent")
    await q.admit("a", "two", 6, 0)
    await refused(q.admit("a", "three", 1, 0), "total_tokens")


async def test_concurrent_terminal_transition_only_once(stores):
    q, other = (manager(s, total=10) for s in stores)
    lease = await q.admit("a", "one", 5, 5)
    results = await asyncio.gather(*(
        (q if i % 2 else other).release(lease, "not_sent") for i in range(50)))
    assert results.count(True) == 1
    await q.admit("a", "two", 10, 0)
    await refused(q.admit("a", "three", 1, 0), "total_tokens")


async def test_conflicting_terminal_calls_are_first_wins(stores):
    q, other = (manager(s, total=10) for s in stores)
    lease = await q.admit("a", "one", 10, 0)
    results = await asyncio.gather(q.release(lease, "cancelled"),
                                   other.release(lease, "not_sent"))
    assert sum(results) == 1
    if results[0]:
        await refused(q.admit("a", "two", 1, 0), "total_tokens")
    else:
        await q.admit("a", "two", 10, 0)


@pytest.mark.parametrize("reason", list(ReleaseReason))
async def test_release_reason_matrix(stores, reason):
    q = manager(stores[0], total=10)
    lease = await q.admit("a", "one", 2, 8)
    assert await q.release(lease, reason)
    if reason == ReleaseReason.NOT_SENT:
        await q.admit("a", "two", 10, 0)
    else:
        await refused(q.admit("a", "two", 1, 0), "total_tokens")
    assert not await q.release(lease, "not_sent")


async def test_unknown_usage_and_oversized_actual_charge(stores):
    q = manager(stores[0], total=10)
    lease = await q.admit("a", "one", 1, 9)
    assert await q.settle(lease, None)
    await refused(q.admit("a", "two", 1, 0), "total_tokens")
    lease_b = await q.admit("b", "one", 1, 1)
    assert await q.settle(lease_b, {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15})
    await refused(q.admit("b", "two", 0, 0), "total_tokens")


async def test_duplicate_pending_and_terminal_ids_cannot_fund_another_request(stores):
    q, other = (manager(s) for s in stores)
    lease = await q.admit("a", "id", 2, 3)
    await refused(other.admit("a", "id", 2, 3), "duplicate_request")
    await q.release(lease, "not_sent")
    error = await refused(other.admit("a", "id", 100, 0), "duplicate_request")
    assert error.retry_after is None
    await other.admit("b", "id", 2, 3)


async def test_forged_lease_cannot_refund_and_policy_mismatch(stores):
    q = manager(stores[0], total=10)
    lease = await q.admit("a", "one", 5, 5)
    for forged in [replace(lease, lease_id="bad"), replace(lease, reserved_tokens=1),
                   replace(lease, client_id="b"), replace(lease, window=0)]:
        with pytest.raises(ValueError, match="mismatched"):
            await q.release(forged, "not_sent")
    await refused(manager(stores[1], total=20).admit("a", "two", 1, 0), "policy_mismatch")
    await refused(q.admit("a", "two", 1, 0), "total_tokens")


async def test_large_exact_integer_roundtrip_and_overflow_closed(stores):
    q = manager(stores[0], rpm=MAX_VALUE, tpm=MAX_VALUE, total=MAX_VALUE)
    lease = await q.admit("a", "one", MAX_VALUE - 1, 0)
    assert lease.input_bound == MAX_VALUE - 1
    assert await q.settle(lease, {"prompt_tokens": MAX_VALUE - 2,
                                "completion_tokens": 0, "total_tokens": MAX_VALUE - 2})
    await q.admit("a", "two", 2, 0)
    await refused(q.admit("a", "three", 1, 0), "total_tokens")
    first = await q.admit("b", "one", MAX_VALUE - 1, 0)
    last = await q.admit("b", "two", 1, 0)
    assert await q.settle(last, {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2})
    await q.release(first, "not_sent")
    await refused(q.admit("b", "three", 0, 0), "account_overflow")


async def test_zero_and_oversized_reservations(stores):
    q = manager(stores[0], rpm=1, tpm=10, total=10)
    error = await refused(q.admit("a", "too-large", 11, 0), "reservation_too_large")
    assert error.retry_after is None
    lease = await q.admit("a", "exact", 10, 0)
    await q.release(lease, "not_sent")
    await refused(q.admit("a", "zero", 0, 0), "rpm")
    no_balance = QuotaManager({"zero": QuotaLimits(10, 10, 0)}, stores[0])
    await refused(no_balance.admit("zero", "one", 1, 0), "total_tokens")
    await no_balance.admit("zero", "zero", 0, 0)
    await refused(q.admit("unknown", "one", 1, 0), "unknown_client")


async def test_window_boundary_delayed_settlement_and_pending_expiry():
    now = [59.25]
    store = MemoryQuotaStore(lambda: now[0])
    q = manager(store, rpm=2, tpm=10, total=100)
    old = await q.admit("a", "old", 5, 5)
    await refused(q.admit("a", "blocked", 1, 0), "tpm", 1)
    now[0] = 60
    current = await q.admit("a", "current", 5, 5)
    await q.release(old, "not_sent")
    await refused(q.admit("a", "still-full", 1, 0), "tpm", 60)
    await q.settle(current, {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})
    await q.admit("a", "rest", 6, 0)
    await refused(q.admit("a", "rpm-full", 0, 0), "rpm", 60)
    now[0] = 120
    await q.admit("a", "new-window", 10, 0)
    # Pending old requests retain cumulative reservation despite many expired windows.
    now[0] = 60000
    await refused(q.admit("a", "too-total", 81, 0), "reservation_too_large")
    # Four + six + ten = twenty consumed. Total 100 leaves exactly 80, across windows.
    for i in range(8):
        now[0] += 60
        await q.admit("a", f"late-{i}", 10, 0)
    now[0] += 60
    await refused(q.admit("a", "total-never-expires", 1, 0), "total_tokens")


async def test_backward_clock_cannot_reopen_old_window():
    now = [120]
    q = manager(MemoryQuotaStore(lambda: now[0]), rpm=1)
    await q.admit("a", "one", 1, 0)
    now[0] = 60
    await refused(q.admit("a", "two", 1, 0), "rpm", 120)


def test_threaded_memory_admission_is_atomic():
    q = manager(total=20)
    def attempt(i):
        try:
            asyncio.run(q.admit("a", str(i), 1, 2))
            return True
        except QuotaRejected:
            return False
    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sum(pool.map(attempt, range(100))) == 6


@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None, MAX_VALUE + 1])
async def test_invalid_inputs(value):
    with pytest.raises(ValueError):
        QuotaLimits(value, 10, 10)
    q = manager()
    with pytest.raises(ValueError):
        await q.admit("a", "one", value, 0)
    with pytest.raises(ValueError):
        await q.admit("a", "one", 0, value)


@pytest.mark.parametrize("usage", [{}, {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}, []])
async def test_invalid_usage_preserves_reservation(usage):
    q = manager(total=10)
    lease = await q.admit("a", "one", 10, 0)
    with pytest.raises(ValueError):
        await q.settle(lease, usage)
    await refused(q.admit("a", "two", 1, 0), "total_tokens")


async def test_validation_configuration_and_reason():
    for seconds in [0, 86401, True]:
        with pytest.raises(ValueError):
            QuotaLimits(1, 1, 1, seconds)
    q = manager()
    for client, request in [("", "id"), ("a", ""), ("a", "字" * 1024), (None, "id")]:
        with pytest.raises(ValueError):
            await q.admit(client, request, 1, 0)
    with pytest.raises(ValueError):
        await q.admit("a", "sum-overflow", MAX_VALUE, 1)
    lease = await q.admit("a", "one", 1, 0)
    with pytest.raises(ValueError):
        await q.release(lease, "anything")
    assert await q.release(lease, "not_sent")


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError, RuntimeError])
async def test_redis_failure_is_closed_and_sanitized(failure):
    class Broken:
        async def eval(self, *args):
            raise failure("secret-auth-key and upstream payload")
    q = manager(RedisQuotaStore(Broken()))
    with pytest.raises(QuotaUnavailable) as caught:
        await q.admit("a", "one", 1, 1)
    assert str(caught.value) == "storage_unavailable"
    assert caught.value.retry_after == 1
    assert caught.value.__suppress_context__


async def test_separate_memory_instances_do_not_share_state():
    a, b = manager(total=1), manager(total=1)
    await a.admit("a", "one", 1, 0)
    await b.admit("a", "one", 1, 0)


async def test_redis_four_processes_share_atomic_budget():
    socket = os.environ.get("GW_QUOTA_REDIS_SOCKET")
    if not socket:
        pytest.skip("real Redis not configured (GW_QUOTA_REDIS_SOCKET)")
    prefix = "gw02:process-test:" + uuid4().hex
    source = '''
import asyncio, sys
sys.path.insert(0, "tests")
from test_quota import RedisWire, manager
from ai_gateway.quota import RedisQuotaStore, QuotaRejected
async def main():
    q = manager(RedisQuotaStore(RedisWire(sys.argv[1]), sys.argv[2]), total=20)
    admitted = 0
    for i in range(25):
        try:
            await q.admit("a", sys.argv[3] + ":" + str(i), 1, 2)
            admitted += 1
        except QuotaRejected as error:
            assert error.reason == "total_tokens", error.reason
    print(admitted)
asyncio.run(main())
'''
    processes = [await asyncio.create_subprocess_exec(
        sys.executable, "-c", source, socket, prefix, str(i),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for i in range(4)]
    results = await asyncio.gather(*(p.communicate() for p in processes))
    for process, (_, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr.decode()
    assert sum(int(stdout) for stdout, _ in results) == 6


async def test_redis_lost_responses_and_terminal_failure_are_conservative(stores):
    store = stores[0]
    if not isinstance(store, RedisQuotaStore):
        pytest.skip("Redis response uncertainty test")
    class LoseResponse:
        def __init__(self, target):
            self.target = target
        async def eval(self, *args):
            await self.target.eval(*args)
            raise TimeoutError("response lost after server applied transaction")
    q = manager(store, total=10)
    uncertain_store = RedisQuotaStore(LoseResponse(store._redis), store._prefix)
    uncertain = manager(uncertain_store, total=10)
    with pytest.raises(QuotaUnavailable):
        await uncertain.admit("a", "one", 10, 0)
    await refused(q.admit("a", "one", 10, 0), "duplicate_request")
    await refused(q.admit("a", "two", 1, 0), "total_tokens")
    lease = await q.admit("b", "one", 10, 0)
    with pytest.raises(QuotaUnavailable):
        await uncertain.release(lease, "not_sent")
    assert not await q.release(lease, "not_sent")
    await q.admit("b", "two", 10, 0)
    await refused(q.admit("b", "three", 1, 0), "total_tokens")


async def test_redis_acl_write_failure_cannot_partially_refund():
    socket = os.environ.get("GW_QUOTA_REDIS_SOCKET")
    if not socket:
        pytest.skip("real Redis not configured (GW_QUOTA_REDIS_SOCKET)")
    admin = RedisWire(socket)
    prefix = "gw02:acl-test:" + uuid4().hex
    username = "gw02-test-" + uuid4().hex
    q = manager(RedisQuotaStore(admin, prefix), total=10)
    restricted = manager(RedisQuotaStore(RedisWire(socket, username), prefix), total=10)
    await admin.command("ACL", "SETUSER", username, "on", "nopass", "+@all", "~" + prefix + ":*")
    try:
        lease = await q.admit("a", "one", 10, 0)
        # Regression: v1 HSETs the balance then SETs the terminal record. Denying
        # SET caused repeated release errors but two refunds (10 -> 0 -> -10).
        await admin.command("ACL", "SETUSER", username, "-set")
        assert await restricted.release(lease, "not_sent")
        assert not await restricted.release(lease, "not_sent")
        await q.admit("a", "two", 10, 0)
        await refused(q.admit("a", "three", 1, 0), "total_tokens")
        # Deny the one actual mutating command: each failure must leave both
        # counters and pending lease unchanged, then recover exactly once.
        other = await q.admit("b", "one", 10, 0)
        await admin.command("ACL", "SETUSER", username, "-hset")
        for _ in range(2):
            with pytest.raises(QuotaUnavailable):
                await restricted.release(other, "not_sent")
        await refused(q.admit("b", "two", 1, 0), "total_tokens")
        await admin.command("ACL", "SETUSER", username, "+hset")
        assert await restricted.release(other, "not_sent")
        assert not await restricted.release(other, "not_sent")
        await q.admit("b", "two", 10, 0)
        await refused(q.admit("b", "three", 1, 0), "total_tokens")
    finally:
        await admin.command("ACL", "DELUSER", username)


@pytest.mark.parametrize("budget", ["rpm", "tpm", "total"])
async def test_each_limit_independently_bounds_concurrent_admission(stores, budget):
    options = {"rpm": 6} if budget == "rpm" else {budget: 20}
    q, other = (manager(store, **options) for store in stores)
    async def attempt(i):
        try:
            await (q if i % 2 else other).admit("a", str(i), 1, 2)
            return True
        except QuotaRejected as error:
            assert error.reason == ("total_tokens" if budget == "total" else budget)
            return False
    assert sum(await asyncio.gather(*(attempt(i) for i in range(100)))) == 6


async def test_explicit_short_window_zero_rpm_and_empty_config():
    now = [14.75]
    q = QuotaManager({"a": QuotaLimits(1, 10, 100, 15),
                      "zero": QuotaLimits(0, 0, 0)}, MemoryQuotaStore(lambda: now[0]))
    await q.admit("a", "one", 1, 0)
    await refused(q.admit("a", "two", 1, 0), "rpm", 1)
    now[0] = 15
    await q.admit("a", "two", 1, 0)
    await refused(q.admit("zero", "one", 0, 0), "rpm")
    await refused(QuotaManager({}).admit("a", "one", 0, 0), "unknown_client")
