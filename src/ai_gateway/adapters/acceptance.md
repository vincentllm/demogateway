# GW-003 frozen acceptance / review input

Source: ai-native-dev-lab/.runs/llm-gateway/{TASKS.md,parallel-contracts.md}; baseline e7c95f0.
Only adapters/ and tests/test_adapters.py may change. Host fallback checkout, not the registered Docker worktree.

| ID | Original requirement / independent oracle | Inputs and states | Verification |
|---|---|---|---|
| A1 | OpenAI and DeepSeek Chat Completions wire contracts | text/tool history, tool choice, JSON/SSE, explicit alias, empty/unknown aliases, mapping/object backend | test_adapters.py request/response/compatible stream assertions |
| A2 | Claude Messages wire contract | leading systems; assistant text plus multiple tool uses; merged tool results; auto/required/none/named tools; missing max_tokens; unsupported fields | test_adapters.py Claude request assertions |
| A3 | Claude JSON mapping | text/mixed/multiple blocks; tool input objects; supported/unsupported stop reasons; cache-inclusive nonnegative token usage | test_adapters.py response assertions |
| A4 | Claude documented SSE sequence | message start; text/tool blocks, sparse block indices; JSON fragments preserved; cumulative usage updates; ping/unknown events; stop/error; truncation and duplicate terminal | test_adapters.py stream assertions |
| A5 | Fail explicitly, no secret propagation | unknown provider/model/field; unsupported text representations; malformed upstream data; HTTP 4xx/429/5xx and stream errors; CRLF key and unsafe URL | test_adapters.py rejection assertions |
| A6 | Pure adapter + compatibility/write boundary | no HTTP or quota calls; per-request state; no edits to existing public modules; no dependencies | source/diff inspection; full existing test suite |
| A7 | Public stream_options behavior | include_usage true/false; accounting usage retained independently; success exactly one DONE, errors never DONE | stream assertions and coverage audit |

No path/config inputs, revision arguments, CLI, remote model loading, quotas or Redis APIs exist in this change; those matrices are excluded. Backend is server-owned configuration, not a client request field. True HTTP/SSE framing and paid-provider tests are integration responsibilities (not evidenced by these unit tests). Review from these requirements first, then separately inspect tests for coverage. Existing tests are not the correctness oracle.

Commands (from this checkout): `PYTHONPATH=src python3 -m pytest tests/test_adapters.py -q`; `PYTHONPATH=src python3 -m pytest -q`; `git diff --check` plus explicit untracked-file inspection. No lab markdown pipeline applies to this Python target. Container workflow state is inaccessible; do not invent node results.
