# RFD-005: Token Counting Strategy: Dummy Tokenizer

| Field | Value |
|-------|-------|
| RFD ID | 005 |
| Title | Token Counting Strategy: Dummy Tokenizer |
| Status | accepted |
| Author | AI Gateway Team |
| Date | 2026-06-11 |

## Summary

Use a pluggable `Tokenizer` interface with a `DummyTokenizer` implementation for the first version. The dummy tokenizer estimates token count from the number of messages and a configurable fixed coefficient.

## Context

Token-level rate limiting requires knowing the approximate token cost of each request before forwarding it to a backend. Different providers use different tokenizers, and integrating a real tokenizer adds dependencies.

## Goals

- Provide a token cost estimate for rate limiting.
- Keep the tokenizer replaceable.
- Avoid heavy external dependencies in the first version.

## Non-Goals

- Exact token counting for billing purposes.
- Provider-specific tokenization.

## Options Considered

### Option A: Dummy Tokenizer

Formula: `token_count = len(messages) * tokens_per_message + overhead`.

**Pros**

- Trivial to implement and reason about.
- No external dependencies.
- User explicitly asked to assume a dummy interface returning token counts.
- The `Tokenizer` interface allows replacement without changing rate limit code.

**Cons**

- Inaccurate for real workloads.
- Does not account for message content length or model-specific tokenization.

### Option B: tiktoken

**Pros**

- Accurate for OpenAI models.
- Industry-standard for GPT-style tokenization.

**Cons**

- Adds a compiled dependency.
- Only works for OpenAI-like tokenizers; not suitable for other providers (e.g., zai).
- User requested a dummy implementation first.

### Option C: Provider-Specific Tokenizer

**Pros**

- Most accurate for each backend.

**Cons**

- Requires a tokenizer implementation per provider.
- Adds complexity before the provider landscape is clear.

## Decision

Use a **pluggable `Tokenizer` interface** with a **dummy implementation**.

The dummy tokenizer is configured via:

```yaml
tokenizer:
  type: dummy
  tokens_per_message: 10
  overhead: 5
```

## Consequences

### Positive

- Rate limit infrastructure is ready for real tokenizers.
- No heavy dependencies.
- Easy to test.

### Negative

- Token-based limits are approximate and may over- or under-count.

## Limitations

- Not suitable for billing or precise quota enforcement.
- Does not vary by model or provider.

## Future Work

- Add a `tiktoken` tokenizer implementation for OpenAI models.
- Add provider-specific tokenizer implementations behind the same interface.
- Allow per-model tokenizer selection in configuration.
