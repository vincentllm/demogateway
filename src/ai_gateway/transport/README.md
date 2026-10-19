# GW-004 transport integration

`StreamingTransport(client, timeout=30.0, max_retries=0,
max_event_bytes=1048576)` borrows a shared `httpx.AsyncClient`. The application
owns and closes the client at shutdown; configure its connection/pool limits at
creation. Transport does not close the client, change model/backend, log request
data, or forward a client's headers. Pass only GW-003's encoded upstream request:
`{method, url, headers, json}`. Use trusted configured URLs and upstream headers.

`stream(encoded, decode_event, on_terminal)` is an async context manager yielding
an async byte iterator. Enter it **before committing downstream response headers**:
it opens HTTP and prefetches one canonical chunk. Keep the context alive for the
entire downstream response and exit it on disconnect, even if iteration never
starts. At ASGI level the lifecycle is:

```python
async with transport.stream(encoded, decode_event, on_terminal) as chunks:
    await StreamingResponse(chunks, media_type="text/event-stream")(scope, receive, send)
```

This belongs inside the response's ASGI call, not a route context that exits when
returning `StreamingResponse`. Before entering, handle `TransportError` by sending
`JSONResponse(error.body, status_code=error.status_code)`. In practice catch errors
from `__aenter__` separately from errors after response start; never send a second
HTTP response after headers. The integration owner owns this route/response wiring.

The synchronous adapter callback `decode_event(event, data, state) -> list` sees
complete SSE events with multiline data joined by newline. Each list item is a
canonical JSON dict or the string `[DONE]`. A provider without that sentinel must
set `state['done'] = True` on its authoritative successful stop event. Empty lists
are allowed for heartbeats and provider metadata. Adapter errors must raise or
return an error dict; raw `event: error` is also treated as failure. Never mark a
provider error as done. EOF without an explicit successful terminal marker is an
incomplete-stream error. Tools remain JSON deltas; transport never assembles them.
GW-003 must confirm this completion signal during integration because the shared
contract does not define its exact representation.

Canonical cumulative usage may come from emitted chunk `usage` or `state['usage']`.
Only nonnegative integer counts with a consistent total are passed to the hook.
Missing/invalid usage is `None`. `on_terminal(usage, outcome)` runs exactly once
after response closure on all paths; a sync or async hook is supported. Outcomes
are `success`, `upstream_error`, `timeout`, `cancelled`. A failure's last observed
usage may be partial: the integration layer must conservatively retain quota when
final usage is unknown. Hook failures propagate after closure; quota/telemetry
hooks must themselves be idempotent, bounded, and safe to run during cancellation.

Every successful stream emits exactly one `data: [DONE]\n\n`. Post-commit failures
emit `event: error` with a sanitized OpenAI error JSON and close without DONE.
Pre-commit HTTP errors preserve status without reading/leaking upstream bodies;
timeouts map to 504 and other transport/adapter errors to 502. Redirects are not
followed. Non-SSE success responses are rejected. UTF-8, CR/LF/CRLF, BOM, comments,
empty/multiline data and split frames are supported; unterminated EOF frames are
discarded. Each event (including ignored fields/comments) has a bounded size.

Default retries are zero. Explicit `max_retries` accepts integers 0..3; only
pre-commit network failures or HTTP 429/502/503/504 can retry the same encoded
request. Retrying may duplicate generation and upstream billing, even without a
downstream chunk. There is no backoff or Retry-After scheduling; the caller may
leave retries disabled. Once the context yields there are no retries or failover.
Only one canonical event is prefetched; subsequent reads follow downstream demand.
