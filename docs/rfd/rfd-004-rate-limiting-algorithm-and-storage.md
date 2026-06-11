# RFD-004: Rate Limiting Algorithm and Storage

| Field | Value |
|-------|-------|
| RFD ID | 004 |
| Title | Rate Limiting Algorithm and Storage |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Introduce a pluggable `RateLimitStorage` abstraction so that rate limit counters can live in memory (single process) or in Redis (distributed). The first algorithm implementation is a fixed window backed by in-memory storage.

## Context

The gateway needs to enforce rate limits. In a single-process demo, in-memory storage is sufficient. In production, multiple gateway instances must share state to enforce global limits. Therefore the storage backend must be swappable without changing the algorithm or facade logic.

## Goals

- Keep the algorithm and storage concerns separate.
- Support in-memory storage out of the box.
- Provide a clear interface for Redis-backed distributed storage.
- Allow switching storage via configuration.

## Non-Goals

- Implementing a production Redis client wrapper with failover and clustering in v0.1.
- Perfectly smooth rate limiting at window boundaries.
- Enterprise-grade rate limiting with sub-second precision.

## Options Considered

### Option A: Algorithm Owns Its Own State

The fixed-window algorithm would maintain its own `dict[key, (window, count)]`.

**Pros**

- Very simple for the first version.
- No extra abstraction.

**Cons**

- Cannot share state across instances without rewriting the algorithm.
- Replacing storage requires changing the algorithm class.
- Harder to unit test storage behavior independently.

### Option B: Pluggable `RateLimitStorage` Interface (Chosen)

Define a storage interface:

```python
class RateLimitStorage:
    async def get(self, key: str) -> tuple[int, int] | None: ...
    async def set(self, key: str, window: int, count: int) -> None: ...
    async def reset(self, key: str | None = None) -> None: ...
```

Implementations:

- `MemoryStorage`: thread-safe in-memory dictionary.
- `RedisStorage`: Redis-backed, uses a Lua script for atomic compare-and-set.

The algorithm receives a `RateLimitStorage` instance at construction time.

**Pros**

- Algorithm and storage are independent.
- Redis support can be added without touching the facade or policy code.
- Storage can be unit tested in isolation.
- Configuration can select `memory` or `redis`.

**Cons**

- Slightly more files and interfaces.
- All storage operations become async.

### Option C: Token Bucket + Redis

**Pros**

- Allows controlled bursts.
- Distributed state across gateway instances.

**Cons**

- Requires Redis deployment.
- Adds latency and failure mode.
- More complex than required for the first version.

### Option D: External Rate Limit Service

**Pros**

- Centralized, scalable, feature-rich.

**Cons**

- Adds a critical dependency.
- Overkill for a prototype.

## Decision

Use **Option B**: a pluggable `RateLimitStorage` interface with a `FixedWindowAlgorithm` that operates on top of it. The first version ships `MemoryStorage`; `RedisStorage` is provided as a placeholder/production-ready implementation behind the same interface.

Configuration example:

```yaml
rate_limiter:
  storage:
    type: memory          # or redis
    redis:
      host: localhost
      port: 6379
      db: 0
      key_prefix: "ai_gateway:rl"
  policies:
    - name: request-limit
      algorithm: fixed_window
      granularity: request
      window_seconds: 60
      limit: 100
```

## Consequences

### Positive

- Storage is now a configuration concern, not a code change.
- Future algorithms (sliding window, token bucket) can reuse the same storage interface.
- Redis support is ready to enable once the `redis` package is installed.

### Negative

- Storage operations are async, so the entire rate-limit chain (`Facade`, `Policy`, `Algorithm`) is now async.
- `RedisStorage` in v0.1 is a single-node implementation; clustering is future work.

## Limitations

- `RedisStorage` assumes a single Redis node and does not handle cluster topology or sentinel failover.
- Fixed window can allow 2x the configured rate at the boundary.
- Memory grows with the number of distinct keys when using `MemoryStorage`.

## Future Work

- Implement `SlidingWindowAlgorithm` and `TokenBucketAlgorithm` behind the same `RateLimitAlgorithm` interface.
- Add Redis connection pooling, sentinel, and cluster support.
- Add storage-level metrics and health checks.
- Add key expiration / TTL for in-memory storage.
