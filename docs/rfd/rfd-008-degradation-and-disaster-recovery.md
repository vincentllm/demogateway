# RFD-008: Degradation and Disaster Recovery

| Field | Value |
|-------|-------|
| RFD ID | 008 |
| Title | Degradation and Disaster Recovery |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Implement retry with backend exclusion, an OpenAI-compatible fallback response when all backends fail, and optional model downgrade mapping for graceful degradation.

## Context

LLM backends can fail or become slow due to rate limits, overloads, or network issues. The gateway must continue to serve clients as gracefully as possible.

## Goals

- Retry transient failures on a different backend.
- Return a sensible response when no backend is available.
- Optionally fall back to a cheaper model when the requested model is unavailable.

## Non-Goals

- Infinite retries or unbounded retry budgets.
- Queueing requests for later processing.
- Generative fallback content (the fallback message is static).

## Options Considered

### Option A: Retry + Exclusion + Static Fallback + Model Downgrade

**Pros**

- Covers the most common failure modes (429, 502, 503, 504, timeout).
- Excluding failed backends from the next attempt improves failover success.
- Static fallback keeps clients from receiving 5xx errors.
- Model downgrade preserves service for non-critical requests.

**Cons**

- Static fallback cannot address context-specific needs.
- Retries increase latency tail.

### Option B: Fail Fast with 5xx

**Pros**

- Simplest implementation.
- Clear semantics for clients.

**Cons**

- Pushes failure handling complexity to every client.
- Breaks OpenAI-compatible clients that expect a 200 response body.

### Option C: Queue and Retry Later

**Pros**

- Can smooth traffic spikes.

**Cons**

- Requires durable queue infrastructure.
- Chat completion clients typically expect synchronous responses.

### Option D: Cached Response Fallback

**Pros**

- Could return a previously successful response for identical prompts.

**Cons**

- Requires cache storage and invalidation logic.
- Risk of returning stale or incorrect answers.

## Decision

Use **Option A**:

- Retry configured status codes up to `max_retries` times.
- Exclude the backend used in each failed attempt from the next attempt.
- If all retries fail, return a static OpenAI-compatible fallback response.
- If configured, attempt a model downgrade after the original model fails.

## Consequences

### Positive

- Improved availability from the client's perspective.
- OpenAI-compatible fallback minimizes client-side changes.
- Model downgrade provides a second chance to satisfy the request.

### Negative

- Retries can increase latency.
- Static fallback may not satisfy users expecting a real LLM response.

## Limitations

- Retry budget is global per request, not per backend.
- Fallback content is fixed; no personalization.
- Circuit breaker is implemented but not yet wired into the routing path.

## Future Work

- Wire `CircuitBreaker` per backend to avoid hammering failing instances.
- Support per-provider and per-model retry policies.
- Explore cached or templated fallback responses.
- Add retry metrics and alerting hooks.
