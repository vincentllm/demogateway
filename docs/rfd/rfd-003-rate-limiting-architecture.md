# RFD-003: Rate Limiting Architecture: Facade + Composable Policies

| Field | Value |
|-------|-------|
| RFD ID | 003 |
| Title | Rate Limiting Architecture: Facade + Composable Policies |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Decompose rate limiting into orthogonal abstractions—algorithm, cost function, key builder, and policy—and compose multiple policies behind a single `RateLimiterFacade`.

## Context

The gateway must support rate limiting at multiple granularities (request count, token count) and must allow switching between strategies without rewriting core logic. The user explicitly requested a Facade pattern with strategy switching.

## Goals

- Support multiple simultaneous rate limit rules (e.g., per-minute request cap + per-minute token cap).
- Allow runtime strategy selection via configuration.
- Keep algorithms and cost calculations independent and reusable.
- Provide a single entry point for the gateway routing layer.

## Non-Goals

- Implementing every possible rate limit algorithm in the first version.
- Distributed rate limit state sharing across gateway instances.

## Options Considered

### Option A: Facade + Composable Policies

Define:

- `RateLimitAlgorithm`: e.g., fixed window, sliding window, token bucket.
- `CostFunction`: e.g., request=1, token count.
- `KeyBuilder`: identity key for the limit scope.
- `RateLimitPolicy`: algorithm + cost function + key builder + limit.
- `RateLimiterFacade`: AND-composes all enabled policies.

**Pros**

- Algorithm and granularity are orthogonal; can combine freely.
- Multiple policies can be active at once.
- Adding a new algorithm or cost function does not change existing code.
- Clean separation between policy definition and enforcement.

**Cons**

- More classes and indirection than a single strategy object.
- Requires understanding of the composition semantics.

### Option B: Single Strategy Object

Create one class per combination, e.g., `FixedWindowRequestLimiter`, `FixedWindowTokenLimiter`.

**Pros**

- Simpler class hierarchy.
- Easier to understand for small, fixed requirements.

**Cons**

- Combinatorial explosion as algorithms and granularities grow.
- Cannot easily enable both request and token limits simultaneously.
- Switching strategies requires replacing the object, not just configuration.

### Option C: Middleware Chain

Implement each rate limiter as a separate FastAPI middleware layer.

**Pros**

- Natural integration with FastAPI.
- Each limiter can short-circuit independently.

**Cons**

- Middlewares are coupled to the HTTP framework.
- Harder to unit test in isolation.
- Composition order becomes implicit and harder to reason about.

## Decision

Use **Facade + composable policies**. The facade exposes a single `allow(request, body)` method, and each policy independently decides whether to allow the request. All policies must pass (AND semantics).

## Consequences

### Positive

- Highly extensible: new algorithms and granularities are plug-and-play.
- Configuration directly maps to the list of active policies.
- Easy to unit test each component in isolation.

### Negative

- Slightly more boilerplate than a single strategy object.
- AND semantics may surprise operators who expect OR or hierarchical limits.

## Limitations

- The first version only implements fixed-window algorithm and request/token cost functions.
- Policy composition is limited to AND semantics; OR and hierarchical composition are not supported.

## Future Work

- Implement sliding window and token bucket algorithms.
- Add support for per-route, per-model, and per-user policy scopes.
- Consider hierarchical limits (e.g., organization cap + user cap).
