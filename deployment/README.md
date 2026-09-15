# Direct Foundry deployment

Business traffic and Logic App health probes call Foundry directly. No Container
App, executor image, shared executor key or intermediary model proxy is required.
See the [design](../docs/monitoring-failover.zh-CN.md).

## Resources and identities

| Resource | Name |
|---|---|
| MCAPS subscription | `10564893-ecc3-4a6d-b505-53bcbe89dd8e` |
| Lab resource group | `rg-svhwb107-apim-lab` |
| Developer APIM | `apim-svhwb107-0915` |
| Gateway | `https://apim-svhwb107-0915.azure-api.net` |
| Log Analytics | `law-svhwb107-0915` |
| Logic App / Action Group | `llm-monitored-failover` |
| Query alerts | `llm-backend-latency`, `llm-backend-errors` |
| Shared model-call UAMI | `id-svhwb107-exec`; client ID `3dfb189c-3492-4eb9-80f8-4bc97213bf76` |
| Model-call principal | `8f1e45f4-0ac1-400d-b403-87ab38dac147` |
| Controller system principal | `f11678b8-01cb-4b24-a17f-584925f02b65` |

The UAMI's historical name is retained to preserve its existing model grants.
It is attached to APIM and Logic App, not an executor. APIM uses
`authentication-managed-identity` with the UAMI client ID. Logic App Foundry
requests select the UAMI resource ID and Cognitive Services audience. ARM route
requests use the workflow's system identity and ARM audience.

Foundry mappings in `config.json`: East US 2 `gpt-5.1`, Sweden
`svhwb107-gpt51`, West US 3 `text-embedding-3-small`. Named APIM backends are
`llm-eastus2`, `llm-sweden`, `llm-embedding`. The shared `backends.py` validates
HTTPS Azure OpenAI origins, fixed IDs and safe deployment names.

## Deployment sequence

These scripts operate on existing lab prerequisites, not a new subscription.
The deployer needs resource write and UAMI attachment permission; assigning model
or management roles still requires an authorized RBAC administrator.

1. During migration, disable controller alerts and ensure no workflow write is
   in flight. Preserve the existing route and quarantine; do not reset primary.
2. Administrator: `bash deployment/grant-model-roles.sh` if the shared model
   identity does not already have the required resource-scoped model grants.
3. `python3 deployment/configure_apim.py` attaches the UAMI, configures named
   Foundry backends and the direct single-attempt policy, and configures diagnostics.
   It preserves existing `chat-route` by default. `--initialize-route` explicitly
   resets East US 2 primary and both enabled; never use for routine redeployment.
4. `python3 deployment/monitoring/deploy.py deploy` attaches both workflow
   identities, configures direct model probes and endpoint-based alert queries.
   Every controller deployment disables alerts and sets `switchEnabled=false`.
5. Complete [controller checks and activation](monitoring/README.md).
   Verify APIM live completions with `python3 deployment/smoke.py`; verify both
   direct workflow model probes without changing the quarantine.
6. Inspect **actual** `ApiManagementGatewayLogs` direct `BackendUrl`/`BackendId`,
   status and timing fields before enabling the revised alerts.

The existing `lab-validation` subscription ID and key are retained. The client
header changes to standard Azure OpenAI `api-key` (not `Ocp-Apim-Subscription-Key`).
The ARM API ID remains `llm` for log attribution, but its public prefix is `openai`.
Configuration removes the four old custom operations after installing native ones.
There is no generated validation API. `llm-policy.xml` is an offline-generated
snapshot of `configure_apim.build_policy()`; importing the module has no cloud effects.

Do not delete the model UAMI, Foundry accounts, Log Analytics, APIM or active
controller as part of executor retirement. The old Container App and its dedicated
ACR/environment can be removed only after direct callers are verified and other
consumers are excluded. Exact actions taken are in the migration record.

## API contract and changed timeout behavior

All paths are POST, protected by `api-key` containing an **APIM subscription key**; get its value through
the authorized APIM interface. Credentials are never stored in source or output.

| Native path | Routing |
|---|---|
| `/openai/deployments/gpt-5.1/chat/completions` | Current chat primary |
| `/openai/deployments/svhwb107-gpt51/chat/completions` | Same chat primary (existing deployment alias) |
| `/openai/deployments/text-embedding-3-small/embeddings` | Fixed West US 3 embedding |

Only the gateway host and, for chat failover across differently named deployments,
the target deployment segment change. APIM does **not read or transform the request
body**, remove `model`, impose a messages schema, or replace `api-version`.
Other business query parameters are preserved. API ID `llm` is unchanged, so the
existing endpoint-attributed alert queries continue to apply.

Foundry validates original parameters and returns its original status/body,
including errors. `stream=true` is allowed and response buffering is disabled
for SSE. The supported scope is the configured chat and embedding deployments,
not all Foundry APIs or `/openai/v1/responses`.

Client credentials, old executor headers and the APIM `subscription-key` query
parameter are removed before managed-identity backend authentication.
The custom `/llm/*` endpoints and operation-specific budgets are retired.
`X-Remaining-Budget-Ms` no longer controls forwarding. `forward-request` waits up
to 120 seconds for response headers; this is not a complete-body deadline.
Caller overall deadlines and cancellation are required.

With an APIM key entered interactively, a native request is:

```bash
(
  set +x
  read -rsp 'APIM subscription key: ' APIM_KEY
  printf '\n'
  printf 'api-key: %s\n' "$APIM_KEY" |
    curl --silent --show-error --include --max-time 130 \
      'https://apim-svhwb107-0915.azure-api.net/openai/deployments/gpt-5.1/chat/completions?api-version=2024-10-21' \
      --header @- --header 'Content-Type: application/json' \
      --data-raw '{"messages":[{"role":"user","content":"Reply only OK"}],"max_completion_tokens":32,"reasoning_effort":"none","stream":false}'
)
```

For SSE, use `stream:true` and curl `--no-buffer`. With the `AzureOpenAI` SDK,
set `azure_endpoint` to the gateway root above, `api_key` to the APIM subscription
key, `api_version` to your supported version, and `model` to `gpt-5.1`.
The SDK still uses native deployment paths; no custom business API is needed.

All attempts are single requests. Timeout/error responses do not reroute themselves.
Logs must distinguish backend responses from gateway failures; no prompt, token,
API key, authorization header or response body logging is required.

## Operations and evidence

### Native API migration, 2026-09-15 11:43 UTC

The final live smoke returned 200 for both chat deployment aliases (Sweden,
2562/2237ms), SSE chat (four data events plus `[DONE]`, 2614ms) and embeddings
(1536 dimensions, 1906ms). Each forwarded once. An invalid caller `api-version`
reached Foundry and returned its 404; an invalid original body returned Foundry's
400 `invalid_type`. The retired `/llm/generate` returned 404 and a native request
without an APIM key returned 401.

Ingested gateway records retain `ApiId=llm`, use `native-eastus2` /
`native-sweden` / `native-embedding` operation IDs, and show the configured
direct backend URLs. The invalid-version record contains the unchanged
`api-version=invalid-version` and backend 404, confirming query passthrough.

Both alert rules and `switchEnabled` were restored to enabled; unchanged-value
MI route access run `08584121342016356741430252673CU37` succeeded. Route remains
Sweden, `enabled=[sweden]`, version 2. No regional failover or recovery was forced.
Migration-time probes encountered transient 503s before the corrected route
guard propagated; final results are not an availability or performance SLA.

SSE data is forwarded without policy buffering. Stream interruptions after a
200 header need client-side handling; existing HTTP-code alerts cannot promise
to detect every partial-stream failure. This is not full body/idle timeout control.

### Earlier executor retirement

The historical 09:17 error-alert drill used the old topology. It establishes prior
control logic behavior, not direct-model credentials or current log attribution.
See [direct migration evidence](monitoring/direct-migration-20260915.md).

Sweden remains primary and East US 2 quarantined until explicit operator recovery.
No enabled backup exists if `enabled=[sweden]`; successful direct probes alone do
not change that. ETag-protected manual re-enablement should preserve the current
primary and other route fields. Disable stops new alert dispatch, not an in-flight
run. Initial configuration must not be used to reset cooldown or quarantine.

Developer APIM, logs, Logic Apps and Foundry continue to incur costs. Latency-alert
drills, N-1 capacity, production HA and Java/Search 8s/15s performance remain separate.
