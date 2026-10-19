# GW-001 authentication module

This module is explicit and synchronous: it does not read files/environment,
change application state, start background workers or forward HTTP requests.
Application wiring is owned by the integration task. Installing this module
alone does not enable authentication on the existing gateway routes.

```python
from ai_gateway.auth import AuthConfig, AuthManager, hash_client_key

# The integration layer loads YAML and resolves provider secrets itself.
# Generate high-entropy random client tokens; store only their SHA-256 digests.
config = AuthConfig.from_mapping({
    "clients": [{
        "client_id": "client-a",
        "keys": [{"key_id": "a-v1", "sha256": hash_client_key(client_token)}],
        "allowed_models": ["gpt-example", "claude-example"],
    }],
    "upstream_pools": [{
        "key_ref": "openai-primary",
        "keys": [{"key_id": "provider-v1", "secret": provider_secret}],
    }],
})
manager = AuthManager(config)
identity = manager.authenticate("Bearer " + client_token, "gpt-example")
provider_secret = manager.resolve_upstream_key("openai-primary")
```

Public configuration dataclasses take keyword arguments only. Sequences are
copied into tuples and model grants into frozensets. Empty configuration fails
closed. Empty model grants deny all models; an explicit `"*"` grants all valid
model names. Permissions match exact configured public model aliases, including
case. `key_id` is globally unique among client keys; `client_id` and `key_ref`
are unique in their respective namespaces. Key IDs are non-secret metadata.
Unknown mapping fields, string booleans, duplicate client digests (even disabled),
and using a client token as a provider secret are configuration errors.

`hash_client_key(raw_token)` is for random API tokens, not passwords. It returns
a lowercase SHA-256 digest. Keys use ASCII HTTP bearer token characters, at most
4096 bytes. Upstream values accept a string or an existing Pydantic `SecretStr`
and are normalized into a redacted `SecretStr`. No implicit environment expansion
occurs: unresolved `${...}` placeholders are invalid. Configuration errors do not
include input values. Default repr and `SecretStr` serialization mask provider
secrets; `resolve_upstream_key` intentionally returns the raw selected secret.
Do not log that result, requests containing headers, or debug locals.

`authenticate(bearer, model)` takes **one complete Authorization header**, not
a raw token. The scheme is case-insensitive; SP separators are allowed. Missing,
malformed, unknown, disabled and revoked credentials all raise the same
`AuthError` (401). Valid credentials with no model grant raise `AuthError` (403).
`error.to_response()` produces an OpenAI-style error JSON; 401 also includes
`WWW-Authenticate: Bearer`. No invalid credential or denied model is echoed.

The caller must reject multiple Authorization headers (for example by passing
`None` unless `len(request.headers.getlist("authorization")) == 1`). Authenticate
before quota accounting or outbound work. Pass `identity.client_id` to quota
storage rather than the original key. Authorize every actual routed/fallback
model; authorizing one alias does not authorize arbitrary model downgrades.
Convert public aliases consistently with the model-routing configuration.
Static backend configuration chooses `key_ref`, never a client-supplied URL/ref.
Supply only the resolved provider key to the adapter; do not copy client headers.

`reload(new_config)` atomically swaps a validated snapshot and resets each pool's
round-robin cursor. For client rotation, first configure old+new enabled keys,
distribute the new token, then disable/remove the old key and reload. The client
identity stays the same across rotation. To revoke a client, set `enabled=False`
on its client entry. Failed parsing/reload leaves the active manager unchanged.
Already-authorized in-flight requests are not cancelled. Reload must be propagated
to every gateway process/replica; this library does not provide distributed config
delivery. Callers should replace their shared manager's snapshot, not cache an old
Identity indefinitely or create one manager per request.

Provider pools select only enabled keys, using thread-safe round-robin per pool.
An unknown, empty or fully disabled pool raises `UpstreamKeyUnavailable` (503),
never a client 401. Rotation is via the same atomic reload. Provider failure
tracking, circuit breakers, key-specific upstream rate budgets and retries are
outside GW-001. All lookups are in-memory; digest lookup is O(1), configuration
validation/reload is O(number of keys). No new dependency is required.

Verification: `node .workflow/run.mjs check`, then `verify` when its task node is
eligible. `tests/test_auth.py` includes a small HTTP consumer with a mock provider
to check status/error serialization and header separation. It does not claim the
unchanged production router is wired or tested for these new behaviors.
