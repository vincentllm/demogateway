# RFD-006: Backend Health and Load Sensing

| Field | Value |
|-------|-------|
| RFD ID | 006 |
| Title | Backend Health and Load Sensing |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Each backend instance maintains a `RollingMetrics` collector. The gateway periodically probes backends via `/health`, classifies them as HEALTHY, DEGRADED, or UNHEALTHY, and routes requests preferring the healthiest available instance.

## Context

The gateway must detect when backend LLM services are under pressure or failing, and adjust routing accordingly. This is essential for resilience and for avoiding cascading failures.

## Goals

- Detect backend unavailability quickly.
- Identify degraded backends before they become fully unhealthy.
- Route traffic away from unhealthy backends automatically.
- Keep the implementation lightweight and self-contained.

## Non-Goals

- Predictive auto-scaling of backends.
- Global cross-instance consensus on backend health.
- Detailed observability dashboards (metrics are exposed via `/admin/backends`, not a full monitoring stack).

## Options Considered

### Option A: Rolling Metrics + Periodic Health Probes + Three-State Classification

Metrics per backend:

- Average latency over a rolling window.
- Error rate over a rolling window.
- In-flight request count.
- Consecutive failures.
- Last success timestamp.

Classification:

- `HEALTHY`: latency and error rate below thresholds, no consecutive failures.
- `DEGRADED`: latency or error rate above threshold, or some consecutive failures, but still responding.
- `UNHEALTHY`: health probe fails repeatedly or too many consecutive failures.

**Pros**

- Lightweight, no external dependencies.
- Three states provide finer control than binary healthy/unhealthy.
- Easy to tune thresholds via configuration.

**Cons**

- Metrics are local to each gateway process.
- Rolling window size affects sensitivity.

### Option B: External Monitoring + Centralized Decision Making

**Pros**

- Global view of backend health across all gateway instances.
- Can use Prometheus, Grafana, or a dedicated control plane.

**Cons**

- Adds operational complexity.
- Health signals have higher latency, making them less useful for real-time routing.

### Option C: Passive Metrics Only

**Pros**

- No health probes needed.
- Simple.

**Cons**

- Slow to detect backend recovery.
- Cannot detect idle backend failures until a real request is sent.

## Decision

Use **rolling metrics + periodic health probes + three-state classification**.

The `BackendPool.select(model, excluded)` method prefers HEALTHY, falls back to DEGRADED, and excludes UNHEALTHY instances.

## Consequences

### Positive

- Fast, local routing decisions.
- Degraded backends can still serve traffic if no healthy ones exist.
- Tuning is configuration-driven.

### Negative

- Each gateway instance has its own view of backend health.
- Threshold tuning requires operational experience.

## Limitations

- Health state is not shared across gateway instances.
- Default `/health` endpoint assumption may not match all providers.
- Rolling windows can lag behind sudden failure spikes.

## Future Work

- Expose metrics in Prometheus format.
- Add circuit breaker integration per backend.
- Support custom health check paths and expected status codes per backend.
- Consider gossip or shared state for multi-instance deployments.
