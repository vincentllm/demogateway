# GW-002 quota lifecycle

```python
from ai_gateway.quota import QuotaLimits, QuotaManager, MemoryQuotaStore

quota = QuotaManager({"client-a": QuotaLimits(rpm=60, tpm=10000, total_tokens=100000)},
                     MemoryQuotaStore())
lease = await quota.admit("client-a", request_id, trusted_input_bound, max_output_tokens)
try:
    # Dispatch exactly once. A duplicate request_id raises duplicate_request.
    usage = await call_upstream()
except BaseException:
    await quota.release(lease, "unknown")  # preserves reservation, including cancellation
    raise
else:
    await quota.settle(lease, usage)  # None also preserves the full reservation
```

The integration owner supplies verified client_id, a globally unique request_id
per upstream attempt, and a **trusted input token upper bound**. This package
does not count tokens. Do not pass a dummy estimate or client-provided identity.
Retries dispatched upstream need a new lease and request id. Request IDs are
deduplicated for the lifetime of the quota store; duplicates are refusals, never
permission to make another upstream request with the old reservation.

All lifecycle methods are async. `admit` returns a frozen `Lease` or raises
`QuotaRejected` (`reason`, `retry_after`). Reasons: `unknown_client`,
`duplicate_request`, `reservation_too_large`, `total_tokens`, `rpm`, `tpm`,
`policy_mismatch`, `account_overflow`; storage errors raise its subclass
`QuotaUnavailable` with `storage_unavailable`, retry_after=1. Only rate-window
refusals have a timed capacity retry; permanent refusals have retry_after=None.
Integration maps these errors to HTTP; no FastAPI/Request dependency lives here.

`settle(lease, usage)` accepts the canonical prompt/completion/total nonnegative
integer mapping (total equals their sum), or None. `release(lease, reason)` takes
`not_sent`, `cancelled`, `timeout`, `upstream_error`, or `unknown`. Both return
True only on the first terminal transition, False on all subsequent valid calls.
A mismatched/unknown lease raises ValueError. Invalid usage/reason raises before
any mutation, leaving the reservation pending. `not_sent` is a caller guarantee
that **no upstream dispatch occurred**, not merely that no downstream bytes were
sent. HTTP failure or cancellation alone does not certify that no tokens were
generated. Known zero usage can be settled with canonical all-zero usage.

Fixed windows are epoch-aligned, 60 seconds by default (configurable 1..86400).
RPM counts admitted attempts, including not_sent attempts; refusal consumes
nothing. Tokens are reserved as input_bound + max_output_tokens in the admission
window and cumulative quota. Actual usage adjusts cumulative tokens and the
admission window if it is still current. Delayed settlement never refunds or
charges the *new* window. TPM therefore describes tokens attributed to admitted
requests in that window, not the wall-clock time when the provider generates
them. Fixed windows allow boundary bursts. Known actual usage above the bound
creates debt and blocks future requests; the caller must prevent unbounded
generation for hard quotas. Unknown completion retains the full reservation.
Pending leases never automatically refund, even after windows expire.

MemoryQuotaStore is atomic across threads/event loops sharing the same object,
but **cannot share quotas across processes, replicas, or separate store objects**.
Use RedisQuotaStore for replicas, with one shared namespace and identical limits.
The first admitted policy is pinned per client; changing limits or window size
requires a coordinated migration, not a hot reload. Zero quotas are valid; a
zero-token reservation may fit a zero-token budget but still requires RPM.
Amounts are exact integers 0..2**53-1 (booleans rejected), including the sum of
input/output bounds. Counters overflowing this range latch admission closed
until operator reconciliation; no silent reset or forgiveness occurs.

RedisQuotaStore takes an injected `redis.asyncio.Redis`-compatible client exposing
`await eval(script, numkeys, *keys_and_args)`. The optional `redis` dependency,
connection pool, timeouts, shutdown, TLS and credentials belong to integration.
No dependency manifests are changed by this worker. Each lifecycle operation
uses one Lua EVAL and **one mutating HSET**, atomically storing account counters
and lease records in a single per-client hash (with a Cluster hash tag). All
serialization happens before that write; a command failure cannot leave counters
changed with a pending lease that could refund again on retry.
Server TIME defines windows; worker clocks are irrelevant. Prefixes must be
distinct from other applications and contain no braces/newlines. Keys hash
client/request IDs; stored lease metadata contains those IDs but no raw API key.
Install source or a wheel normally: Lua is embedded in a Python module so it
does not require package-data configuration.

Redis exceptions are sanitized and **fail closed**; there is no memory fallback.
A timeout may mean the operation was applied. Never dispatch on failed admission;
retry reconciliation with the same request id must not dispatch a duplicate.
Cancellation or failure during settlement leaves the reservation conservative;
retrying the terminal call is safe. Redis persistence, `maxmemory-policy noeviction`,
appropriate ACLs and capacity are prerequisites. Failover that loses acknowledged
writes can lose quota state; strict consistency across such failover is outside
Redis EVAL's guarantee. Lua runtime failures are not rollback transactions;
namespace/type corruption or OOM needs operator reconciliation, not auto-reset.
Do not split account and lease writes into separate commands inside the script.

Account totals and request ledgers have **no TTL**. Keeping tombstones is deliberate:
expiry cannot both preserve forever-idempotency and allow arbitrary old request
ids to be retried. Storage grows O(clients + admitted requests), operations are
O(1) per request (no ledger scan). Provision storage and schedule explicit account
archival/migration after requests drain; no automatic ledger cleanup is supplied.
Changing namespace creates a fresh quota, so do not use it as an outage fallback.

Tests use deterministic memory clocks and a minimal real RESP test client; real
Redis tests run only when `GW_QUOTA_REDIS_SOCKET` names a dedicated local server.
No live provider or credentials are needed. The optional production Python Redis
client is not required to run the real-server Lua tests.
