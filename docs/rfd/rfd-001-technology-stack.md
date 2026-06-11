# RFD-001: Technology Stack: Python + FastAPI

| Field | Value |
|-------|-------|
| RFD ID | 001 |
| Title | Technology Stack: Python + FastAPI |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Choose Python 3.11+ with FastAPI as the implementation language and framework for AI Gateway.

## Context

AI Gateway is a network-facing HTTP service that must:

- Expose an OpenAI-compatible `/v1/chat/completions` endpoint.
- Perform async I/O to backend LLM providers.
- Support request streaming (Server-Sent Events).
- Provide Pydantic-style request/response validation.
- Be easy to iterate on while the architecture stabilizes.

The primary candidates were Python/FastAPI for rapid prototyping, Go for balanced performance, and Rust/Pingora for high-performance production use.

## Goals

- Rapid development and iteration.
- Strong async HTTP client/server ecosystem.
- Easy integration with LLM provider SDKs and tokenizer libraries.
- Clear, self-documenting APIs (OpenAPI).

## Non-Goals

- Maximizing raw throughput or minimizing latency to the absolute theoretical limit.
- Compiling to a single static binary for edge deployment.

## Options Considered

### Option A: Python + FastAPI

**Pros**

- FastAPI provides native async support, dependency injection, and automatic OpenAPI docs.
- httpx is a mature async HTTP client suitable for proxying streaming responses.
- Pydantic integrates cleanly for configuration and request validation.
- The LLM ecosystem (OpenAI SDK, tiktoken, etc.) is primarily Python-first.
- The team already has Python 3.11 available; no new toolchain installation required.

**Cons**

- Python's GIL limits true CPU parallelism per process.
- Lower raw throughput and higher latency tail compared to Go/Rust for pure proxy workloads.
- Dynamic typing can hide errors without thorough test coverage.

### Option B: Go

**Pros**

- Faster compilation and execution.
- Goroutines provide lightweight concurrency without async/await boilerplate.
- Static binary deployment is simpler.

**Cons**

- Go was not installed in the target environment at project start.
- Re-implementing Pydantic-style validation and OpenAPI generation is more verbose.
- The LLM ecosystem has fewer native libraries compared to Python.
- Slower iteration speed while the architecture is still exploratory.

### Option C: Rust + Pingora

**Pros**

- Best-in-class throughput and latency for an HTTP proxy/gateway workload.
- Pingora provides a programmable, memory-safe proxy framework built on Tokio.
- Strong type system catches many errors at compile time.
- Ideal for production-grade high-performance gateways.

**Cons**

- Rust toolchain and Pingora learning curve are steep.
- Compilation times are significantly longer than Python or Go, slowing down iterative prototyping.
- Smaller ecosystem for LLM-specific integrations compared to Python.
- Overkill for a quick demo and architecture validation phase.

## Decision

Use **Python 3.11+ with FastAPI** for the first version.

This choice is intentionally optimized for **speed of iteration and demo delivery**, not for ultimate production performance. The goal is to validate the gateway architecture (rate limiting, backend health, provider abstraction, resilience) without waiting for compilation or wrestling with low-level proxy frameworks. For a high-throughput production gateway, **Rust + Pingora** would be the stronger long-term choice.

## Consequences

### Positive

- Faster time-to-first-working-gateway.
- Rich ecosystem for OpenAI-compatible protocols and tokenizers.
- Automatic OpenAPI documentation at `/docs`.

### Negative

- CPU-bound work (e.g., token counting, response transformation) must be kept small or offloaded.
- Higher memory footprint per connection than Go.

## Limitations

- Single-process throughput is bounded by Python's event-loop performance.
- Multi-instance deployments require a load balancer in front.
- Python is **not the right choice for a high-throughput production gateway**; this implementation is a prototype and demo vehicle.

## Future Work

- If the project moves to production, rewrite the gateway in **Rust + Pingora** (or Go) for performance and memory safety.
- Until then, keep the core abstractions (`Provider`, `RateLimitAlgorithm`, `CostFunction`) language-agnostic to simplify a future migration.
- Profile the Python implementation to identify the exact hot paths that would benefit most from a compiled rewrite.
