# Running the integrated gateway

`platform` configuration activates the composed authentication, quota, protocol
adapters, streaming transport and observability implementation. Configuration
without it retains the legacy demonstration API; use `config.demo.yaml` for the
complete platform. That file contains deliberately public fixture credentials.
Bind the demo to a trusted development environment only.

Install dependencies with `uv sync --frozen --extra dev`. Start a dedicated Redis
instance at `/tmp/gw-integration-redis.sock` (or set `GATEWAY_REDIS_URL`). Then run:

```sh
uv run uvicorn scripts.mock_multi_provider:app --host 127.0.0.1 --port 9999
GATEWAY_CONFIG=config.demo.yaml uv run uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000
GATEWAY_CONFIG=config.demo.yaml uv run uvicorn ai_gateway.main:app --host 127.0.0.1 --port 8001
uv run python scripts/e2e_gateway.py --second-url http://127.0.0.1:8001
```

Use a fresh `GATEWAY_QUOTA_NAMESPACE` for each acceptance run on both gateway
processes: the limited fixture client intentionally permits only one request per
minute and its permanent lease ledger is not cleared by this script. Never clear
a production quota namespace to rerun development tests.

For repeatable isolated acceptance, run `uv run python scripts/run_e2e.py`. It
starts its own provider fixtures and two gateway processes on free ports, uses a
fresh Redis namespace, saves evidence under `.runs/e2e/`, and stops only processes
it created. Existing services continue running. Redis remains externally managed.

The full E2E uses actual HTTP sockets and three deterministic fake providers.
Fixtures enforce provider-side credentials, emit provider-specific text/tool
streams, and track active upstream connections. The script asserts canonical
output and usage, rejection before dispatch, terminal framing, first-delta timing,
cancellation before/after prefetch, safe errors, two-replica Redis admission and
zero active metrics. It does not establish live vendor compatibility under all
vendor extensions, production throughput, or production fault tolerance.

`input_token_bound` is an operator-supplied trusted upper bound on accepted input
tokens for that route, normally the provider's enforced maximum context size.
Admission reserves that bound plus the bounded requested output. This deliberately
reduces usable concurrency; a heuristic tokenizer cannot enforce a hard quota.
Validate model/context settings when changing a route. Successful canonical usage
settles actual cost; failures, cancellations and missing usage conservatively
retain the reservation. Provider reporting beyond the reservation becomes debt.

For horizontal scaling, all replicas must use the same Redis endpoint, namespace,
client limits, key grants and model routes. Memory storage enforces only one
process. Configure finite HTTP connection limits/timeouts, shared secret injection,
load-balancer streaming passthrough and generous idle timeout. Remove a replica
from traffic before terminating it; allow active streams to finish. Redis quota
ledger tombstones require capacity planning and an explicit retention/migration
policy; automatic expiration would weaken deduplication and refund safety.

Scrape `/metrics` on a private network; logs contain projected lifecycle metadata.
See `observability.md` and `load-testing.md` for metric definitions and load CLI.
Run load clients outside the gateway container when measuring deployment capacity;
record client resources, configured caps, model/provider delays, errors, success
throughput, latency and meaningful TTFT. Test JSON, SSE and mixed workloads and
repeat against one and multiple replicas sharing Redis. Do not interpret fixture
latencies as live model performance.
