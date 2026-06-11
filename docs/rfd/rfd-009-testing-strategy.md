# RFD-009: Testing Strategy: MockTransport-Based Integration Tests

| Field | Value |
|-------|-------|
| RFD ID | 009 |
| Title | Testing Strategy: MockTransport-Based Integration Tests |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Use unit tests for individual modules and integration tests that wire the real Gateway to a `MockLLMBackendSender` via `httpx.MockTransport`. A standalone mock backend server is also provided for manual demos.

## Context

Testing a gateway requires validating both internal logic (rate limiting, backend selection) and end-to-end request forwarding. Real LLM backends are costly and unpredictable, so tests must be deterministic and fast.

## Goals

- Fast, deterministic tests without real network calls.
- High coverage of gateway logic and failure paths.
- Easy manual testing with a mock backend process.

## Non-Goals

- Load testing or performance benchmarking in v0.1.
- Chaos testing against production-like infrastructure.

## Options Considered

### Option A: httpx.MockTransport + MockLLMBackendSender

**Pros**

- No real network calls; tests are fast and deterministic.
- Can simulate latency, failures, streaming, and health states.
- The gateway code does not need test-specific hooks.
- Mock backend can also be exposed as a standalone server.

**Cons**

- Slightly less realistic than a real HTTP subprocess.
- Transport-level behavior may differ from real sockets in edge cases.

### Option B: Real Subprocess Backend

**Pros**

- Closer to production.

**Cons**

- Slower test startup and teardown.
- Harder to inject failures and latency deterministically.
- Port management and process cleanup add complexity.

### Option C: Record/Replay (VCR)

**Pros**

- Can capture real provider responses once and replay them.

**Cons**

- Recordings become stale as providers change.
- Harder to simulate failure scenarios and streaming.

## Decision

Use **Option A** for automated tests, plus a standalone `scripts/mock_backend_server.py` for manual demos.

The test suite includes:

- Unit tests for rate limiting, backend metrics, and resilience helpers.
- Integration tests that create a real `FastAPI` app with a mocked httpx client.
- Manual scripts to start a mock backend and send requests through the gateway.

## Consequences

### Positive

- Tests run in milliseconds.
- Failure scenarios are easy to reproduce.
- The same `MockLLMBackendSender` is reused across unit, integration, and manual tests.

### Negative

- Tests do not exercise real TCP sockets or TLS.
- Subtle transport differences may not be caught.

## Limitations

- Performance characteristics differ from real network calls.
- Provider-specific quirks must be mocked explicitly.

## Future Work

- Add a small set of subprocess-based smoke tests for release validation.
- Introduce load tests once performance requirements are defined.
- Consider contract tests against real provider sandboxes.
