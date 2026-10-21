# Gateway integration acceptance

User request: finish GW-005 repairs, merge reviewed changes, start the complete
gateway in the Dev Container and exercise full E2E. Prior module-only checkpoint
is `0b03e31`; it did not wire the new modules to HTTP routes.

## Frozen acceptance and input/state matrix

| ID | Requirement and independent oracle | Verification |
| --- | --- | --- |
| OBS-1 | Arbitrary upstream/client text and credentials cannot appear in structured lifecycle logs; only bounded structured error fields survive. | `tests/test_observability.py` |
| OBS-2 | Each request increments/decrements the same gauge series exactly once, including duplicate finish and late first-token calls. | Prometheus samples + lifecycle tests |
| OBS-3 | Default metrics registry exports real Prometheus text, instances isolate state, unknown route labels collapse to a fixed pair. | Prometheus parser + cardinality regressions |
| LOAD-1 | JSON-only/empty text reports work; nonfinite/negative CLI values fail cleanly. | `tests/test_load_test.py` |
| LOAD-2 | TTFT begins at a meaningful text/tool delta, not heartbeat/role; HTTP errors and incomplete/broken streams cannot count as successful SSE. | deterministic clock and protocol fixtures |
| E2E-1 | Legacy configuration remains compatible; platform configuration activates real modules and validates startup. | full pytest suite + runtime tests |
| E2E-2 | Invalid key, model grant denial, duplicate auth and invalid request cause no provider dispatch. Provider fixtures reject forwarded client keys. | real HTTP E2E + runtime tests |
| E2E-3 | OpenAI, DeepSeek and Claude return canonical text and tools in JSON and SSE, alias preserved, usage normalized, one terminal DONE on success. | `scripts/e2e_gateway.py` against actual TCP servers |
| E2E-4 | Error before headers returns safe HTTP error; after headers emits safe SSE error without DONE. First delta is delivered before terminal; disconnect during prefetch or after first delta closes upstream. | real HTTP E2E + runtime transport tests |
| E2E-5 | Configured conservative input bound and output limit fund admission. Success settles actual usage; unknown/error/cancel retain reservation. Redis shares admission across two gateway processes. | quota/runtime tests + two-process HTTP E2E |
| E2E-6 | `/metrics` exports bounded safe labels; all completed/disconnected requests leave active gauge at zero. | E2E metrics assertions + log sentinel scan |
| OPS-1 | Container loads the merged source and keeps demo gateway running; instructions describe performance test and Redis-based horizontal scaling limits honestly. | container source revision, check/verify logs, HTTP health and docs review |

Input inventory: missing/duplicate/disabled/valid credentials; allowed/forbidden/
unknown aliases; text/tools, JSON/SSE, missing/explicit usage; valid/invalid/large
request; success/upstream HTTP failure/midstream failure/truncation/timeout;
disconnect before first frame/after frame; memory/Redis shared admission;
empty/populated metrics and reports. No live paid provider calls are authorized
or needed for deterministic protocol E2E. Production distributed load, real model
tokenizer/context guarantees, Windows and Redis Cluster remain unverified.

Implementation owners: isolated GW-005 worktree owns observability and load
script; isolated integration worktree owns runtime/config/entrypoint; root owns
dependencies, task record, real HTTP fixtures, merge and container deployment.
After freezes, separate requirements-first reviews precede commit and merge.
