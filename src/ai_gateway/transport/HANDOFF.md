# GW-004 handoff — implementation ready, real HTTP acceptance BLOCKED

## Checkout and scope

- Baseline: `e7c95f0` from `/Users/Jackl/ai-gateway`.
- Branch: `task/llm-gateway-streaming`.
- Actual isolated checkout: `/Users/Jackl/workspace/llm-gateway-streaming`.
- Registered container checkout: `/workspaces/workflow/worktrees/llm-gateway-streaming`
  in `e49a2a6b17e7`; Docker socket access was denied in this sandbox. This is a
  host clone, not the registered container worktree. Do not assume container files
  have changed.
- No commits, pushes, dependencies, shared config/router/providers, contracts or
  global tracker changes. All implementation files are untracked additions;
  integration must copy/apply them explicitly.
- Files: `src/ai_gateway/transport/{__init__.py,streaming.py,README.md,HANDOFF.md}`,
  `tests/test_transport.py`, `tests/test_streaming_http.py`.

## Requirement and input/state review matrix

This is a separate self-review phase against the original GW-004 requirements,
followed by a test coverage audit; it is not a second-person review or integration
approval. Source was frozen after the fixes listed below. No existing public
signature, positional argument, dataclass, exception or serialization was changed.
New signatures and integration assumptions are documented in README.md.

| ID | Original requirement / independent expected behavior | Inputs/states and concrete verification | Result |
| --- | --- | --- | --- |
| S1 | Incremental SSE framing preserves event/data | UTF-8 byte widths 1/2/7/1000, BOM, CR/LF/CRLF, comments, empty event/data, multiline data, partial EOF, invalid UTF-8 and size cap; exact expected tuples in `test_framing`, `test_empty_event_type_is_message`, `test_bounded_and_invalid_utf8` | PASS |
| S2 | Nonbuffered shared-client transport and backpressure | First event reads one source item, no further reads until demand; caller-owned client stays open; `test_success_usage_and_backpressure`. Real first-event-before-finish uses a release gate in `test_first_event_arrives_before_upstream_completion` | Unit PASS; real HTTP BLOCKED |
| S3 | Distinct pre/post commit failures, no false success | HTTP 401/429/500/503 before context yields, real status retained and body unread; malformed JSON, provider errors, wrong media type and empty EOF rejected. Post-first-chunk read timeout/read error/EOF emits error without DONE | PASS at fake HTTP boundary; real read timeout BLOCKED |
| S4 | Explicit bounded retry, no replay after commit | Default zero, configured two retries reaches third attempt; connect error/timeout stops at configured limit; HTTP-200 provider error not retried; midstream failure with max_retries=3 still one attempt | PASS |
| S5 | Close upstream on cancellation; no connection or concurrency leak | Cancellation in prefetch and iteration, repeated cancellation during slow async hook, context exit before consuming, hook failure: source closed and single hook. Real pool capacity one across three aborted requests in `test_disconnect_releases_connection_and_pool_slot` | Unit PASS; real pool/connection BLOCKED |
| S6 | Structured single terminal, canonical usage, exactly one DONE | Success/no usage/canonical usage, async hook and adapter-state completion, upstream_error/timeout/cancelled outcomes; exact terminal list and DONE counts | PASS |

Exclusions: input paths, revision options and singular/plural configuration do not
apply to this new dict-based HTTP/SSE interface. Provider tool assembly is owned
by GW-003; adapters receive complete events but tool deltas remain dictionaries.
Quota/telemetry invocation, inbound disconnect detection and ASGI response headers
are integration responsibilities; this module exposes lifecycle hooks and context
cleanup rather than editing the shared route. No claims about provider compatibility
or production throughput are made from these tests.

## Review findings and restart

1. Empty SSE event field initially yielded an empty event type. Evidence: parser
   assigned `content` directly; SSE default dispatch type is `message`. Fixed to
   `content or 'message'` with an exact-tuple regression test.
2. Retry selection initially checked the sanitized exception's default status 502,
   which could retry a provider error carried over HTTP 200. Fixed to inspect the
   actual upstream HTTP status; `test_provider_error_on_http_200_is_not_retried`
   asserts one attempt with retries configured to three.

After both fixes, reviewed again from original requirements: prefetch/commit
boundary, explicit completion vs truncated EOF, timeout classification, borrowed
client ownership, finally cleanup, cancellation and hook cardinality, sanitized
exceptions/payloads, per-stream state and bounded parser memory. No additional
concrete defect was identified in this pass. Passing tests cover only the cases
listed here.

Frozen SHA-256 values (excluding documentation):

```text
d758ce7f3c3c0a5f9c94af251ed4d0dcafa4a2c3f86a1b00fd2d2ec2f33b47ad  src/ai_gateway/transport/streaming.py
0b98ee68a71eaf30bfbd5b292bca9202737a7ad507d93c3f9e92ee59c360e9f2  src/ai_gateway/transport/__init__.py
0096f97792f826a58e37b4201be01d92a97b5b903bed9ee24f0c08819cbac5e5  tests/test_transport.py
b26178f5fe6e6defd8ed65d345e27ec97baf8990cc2b11e0a519bb881b96c669  tests/test_streaming_http.py
```

## Actual commands and evidence

Run from the actual checkout, using existing host dependencies without copying
credentials or installing anything:

```sh
PYTHONPATH=src /Users/Jackl/ai-gateway/.venv/bin/python -m pytest -q
# Before implementation: 27 passed, 1 warning.

PYTHONPATH=src /Users/Jackl/ai-gateway/.venv/bin/python -m pytest -q --ignore=tests/test_streaming_http.py
# Final candidate: 55 passed, 1 warning (27 baseline + 28 transport tests).

PYTHONPATH=src /Users/Jackl/ai-gateway/.venv/bin/python -m pytest -q tests/test_streaming_http.py
# 4 failed before exercising transport:
# PermissionError: [Errno 1] error while attempting to bind on address
# ('127.0.0.1', 0): [errno 1] operation not permitted
```

The unchanged warning is Starlette's existing TestClient/httpx deprecation.
The four real HTTP tests are retained as runnable tests, not skipped or weakened.
The ignore command deliberately excludes BLOCKED tests and is not a full-suite
pass. They must run successfully in a socket-enabled environment before accepting
GW-004. In the registered container, use its own Python environment and run
`PYTHONPATH=src python -m pytest -q` after applying the patch.

## Remaining risks / integration requests

- Confirm adapter completion representation (`[DONE]` or `state['done']`) with
  GW-003; shared contracts only specify a list of canonical payloads, not the
  exact provider-completion signal. The fake adapter proves this module's local
  convention, not cross-module compatibility.
- Keep context alive through ASGI streaming and disconnect cleanup; enter before
  headers so prefetch errors can retain HTTP status. No end-to-end downstream
  ASGI disconnect or real provider test has run.
- Non-success usage may be partial; quota must retain reservations when final
  billable usage is unknown. Hooks must be bounded and idempotent.
- Retrying can duplicate paid generation. Retries default off; no backoff or
  Retry-After scheduling is implemented. No route/model switching occurs.
- Real sockets, real pool reuse, container/Linux, Windows, paid suppliers,
  Python 3.11 minimum runtime and production throughput remain unverified.
- No unrelated baseline failures were observed. Do not mark the shared task done
  from this handoff alone; the integration owner must rerun acceptance and review.
