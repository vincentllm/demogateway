# RFD-002: Configuration: Static YAML

| Field | Value |
|-------|-------|
| RFD ID | 002 |
| Title | Configuration: Static YAML |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Use a static YAML file as the sole configuration source for AI Gateway, with environment variable substitution for secrets and per-environment values.

## Context

AI Gateway needs declarative configuration for:

- Server bind address and port.
- Rate limiting policies (multiple algorithms and granularities).
- Backend LLM provider instances.
- Resilience rules (retries, fallback, model downgrade).
- Health check and load metric thresholds.

A configuration system should be easy to read, version-control, and deploy.

## Goals

- Human-readable, diff-friendly configuration.
- No external dependencies required to start the gateway.
- Support for secrets via environment variables.
- Easy to validate at startup with Pydantic.

## Non-Goals

- Dynamic configuration updates without restart.
- Multi-tenant or per-request configuration overrides.
- Complex secret management (e.g., Vault integration).

## Options Considered

### Option A: Static YAML with Environment Variable Substitution

**Pros**

- Simple to read and maintain.
- Works well with GitOps and version control.
- Supports `${VAR:default}` syntax for secrets and environment-specific values.
- Pydantic can validate the entire file on startup.

**Cons**

- Backend changes require a process restart.
- No history or rollback built into the gateway itself.

### Option B: Dynamic Service Discovery (Consul / etcd)

**Pros**

- Backend instances can register and deregister dynamically.
- Configuration can be updated without restarting the gateway.
- Suitable for large, auto-scaling clusters.

**Cons**

- Adds operational complexity (another service to run and monitor).
- Overkill for the first version, which targets a small, fixed set of backends.
- Increases test complexity.

### Option C: Environment Variables Only

**Pros**

- Common in containerized deployments.
- No file parsing needed.

**Cons**

- Becomes unwieldy for nested structures like rate-limiting policies and backend lists.
- Hard to review diffs.

## Decision

Use **static YAML with environment variable substitution**.

The gateway looks for configuration in the following order:

1. `GATEWAY_CONFIG` environment variable.
2. `config.yaml` in the working directory.
3. `/etc/ai-gateway/config.yaml`.

## Consequences

### Positive

- Configuration is explicit and version-controllable.
- No network dependency at startup.
- Secrets are not committed to the repository.

### Negative

- Configuration changes require a restart.
- Operators must manage YAML files across environments.

## Limitations

- Hot reload is not implemented.
- Multi-instance deployments each load their own copy; consistency is the operator's responsibility.

## Future Work

- Add a configuration reload endpoint or file watcher.
- Consider integrating with Consul/etcd when dynamic backend registration becomes a requirement.
- Add JSON Schema or OpenAPI-based validation documentation for operators.
