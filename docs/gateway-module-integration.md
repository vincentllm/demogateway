# Gateway module integration contract

This integration merges completed, reviewable module deliveries; it does not enable the modules in the existing application router. Source: the GW-001/GW-003/GW-004 worker handoffs and public contracts v1. GW-002 v2 has a frozen six-file delivery, real Redis evidence and a separate independent review; its imported commit is included. GW-005 has no module implementation delivery and remains excluded. Historical handoffs describe their original session, not the current merge status.

Frozen requirements and state matrix before integration fixes:

| ID | Requirement | Independent assertion / verification |
|---|---|---|
| I1 | Preserve module commits and merge history | GW-004 92de1206d7b033e20c87b470d56e67a130aecf4f must remain an ancestor; separate GW-001/GW-003 commits; merge commits preserve all contributing parents (including the independent heads of an octopus merge); no squash/rebase/force push |
| I2 | Retain worker behavior and original tests | Run full suite; run all 4 real socket tests in the Dev Container, and run quota tests against an isolated real Redis; never remove or weaken them |
| I3 | Adapter callback works with transport-owned fresh dict | All 3 providers: text, tools, usage, one DONE; error and truncated streams produce no success; independent known SSE wire fixtures exercise both real modules through MockTransport |
| I4 | Authentication separates identities and provider keys | Authenticate fake client token, resolve separate fake upstream pool key; encoded request header contains only upstream token; denied identity cannot encode |
| I5 | Truthful delivery state and compatibility | Existing router/config/model signatures untouched; module readiness does not imply active auth/quota/telemetry; report remaining tests, inaccessible source checkouts and remote push outcome |

No live credentials or providers are required. Mock HTTP integration establishes callback composition, not real socket lifecycle or production assembly. New adapter initialization must preserve caller-supplied state and the existing new_state include_usage/model arguments. No CLI/path/revision APIs are introduced. Local main is this workspace checkout; the original /Users/Jackl/ai-gateway main is synchronized only if permissions allow. Historical worker handoffs remain initial snapshots and are not current commit/push status reports.

## Running this checkpoint

The completed auth, quota, adapters and transport packages are merged as reusable modules. They have not yet been assembled into the existing `ai_gateway.main:app` route. This checkpoint therefore runs the existing mock gateway for an HTTP smoke test, while the module suite separately exercises real streaming sockets and Redis. It does not claim active authentication, cumulative quotas, multi-provider routing or telemetry on the old route.

Inside the loaded Dev Container project:

```sh
cd /workspaces/workflow/worktrees/llm-gateway
uv sync --frozen --extra dev
.venv/bin/python scripts/mock_backend_server.py --port 9999
# Separate terminal:
GATEWAY_CONFIG=config.mock.yaml .venv/bin/python -m uvicorn ai_gateway.main:app --host 0.0.0.0 --port 8000
```

The gateway health endpoint is `/health`, and its OpenAI-style chat endpoint is `/v1/chat/completions`. Mock model: `gpt-4o-mini`. These local demos require no paid provider key. Use a newly created test Redis socket for quota acceptance, as documented in quota/README.md; do not share the worker's running Redis instance. Final commands, test counts and smoke responses are local evidence rather than capacity claims.
