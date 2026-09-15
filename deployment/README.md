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

`config.json` contains only East US 2 and Sweden origins. Their deployment names
(`gpt-5.1` in both regions) are used by direct health probes, log attribution
and smoke requests, never APIM rewrites. Named APIM backends are `llm-eastus2`
and `llm-sweden`. The shared `backends.py` validates the origins and probe names.

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
The ARM API ID remains `llm` for log attribution. Its public prefix is empty:
seven method-specific `/*` operations proxy arbitrary paths at the gateway root.
Configuration refuses to take over an existing root API. Other APIs on this APIM
with a more-specific prefix still take precedence; the wildcard does not replace them.
There are no exact-path operations or compatibility routing exceptions.
There is no generated validation API. `llm-policy.xml` is an offline-generated
snapshot of `configure_apim.build_policy()`; importing the module has no cloud effects.

Do not delete the model UAMI, Foundry accounts, Log Analytics, APIM or active
controller as part of executor retirement. The old Container App and its dedicated
ACR/environment can be removed only after direct callers are verified and other
consumers are excluded. Exact actions taken are in the migration record.

## API contract and changed timeout behavior

GET, POST, PUT, PATCH, DELETE, HEAD and OPTIONS accept arbitrary paths, protected
by `api-key` containing an **APIM subscription key**; get its value through
the authorized APIM interface. Credentials are never stored in source or output.

Every request uses the current primary origin; only the target origin changes. No prefix is added,
removed or inferred: `/v1/chat/completions` stays `/v1/chat/completions`,
`/openai/v1/responses` stays `/openai/v1/responses`, and `/anything` stays `/anything`.
APIM does **not read or transform the request
body**, remove `model`, impose a messages schema, or replace `api-version`.
Other business query parameters are preserved, including repeated values.

Foundry validates original parameters and returns its original status/body,
including errors. `stream=true` is allowed and response buffering is disabled
for SSE. Binary/multipart bodies are not parsed by policy either. HTTP content
framing, hop-by-hop headers and URL normalization still follow APIM behavior;
this is not a byte-for-byte TCP tunnel. CONNECT/TRACE, WebSockets and gRPC are
not configured by this HTTP proxy.

**All paths are proxied; not all APIs are implemented by the upstream.** Current
trusted origins remain the existing Foundry accounts, with Azure managed identity.
A `/v1/messages` request reaches that origin unchanged, not Anthropic, and may
return 404. An OpenAI/Anthropic/custom upstream requires operator configuration
of its trusted origin and authentication plus corresponding health probes and
monitoring rules; it cannot be selected through a caller-supplied URL or Host.
The current Azure-specific deployment helper deliberately retains its origin
allowlist. No external providers or credentials were provisioned in this change.

Both URL deployment names and body `model` must already name a deployment available
at the current upstream (`gpt-5.1` in both regions). There are no aliases or
embedding routing exceptions. Safe cross-region failover requires compatible deployment
names/models on both sides. Stateful files/jobs/response IDs are not replicated
between regions; forwarding arbitrary APIs does not make them region-portable.
APIM subscription holders can now reach all upstream data-plane paths allowed
by the model identity, including write/delete paths, not only inference.

Client credentials, old executor headers and the APIM `subscription-key` query
parameter are removed before managed-identity backend authentication.
The custom `/llm/*` business handlers and operation-specific budgets are retired;
those paths now go to the upstream through the wildcard like any other path.
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

The standard OpenAI SDK can use
`base_url=https://apim-svhwb107-0915.azure-api.net/openai/v1/`,
`default_headers={"api-key": APIM_SUBSCRIPTION_KEY}`, and
`model="gpt-5.1"` for either upstream. Its Bearer Authorization
is not the gateway credential: `api-key` is required and backend auth uses MI.
For curl, replace the native URL above with `/openai/v1/chat/completions` and
include `"model":"gpt-5.1"` in the original JSON body. No api-version is
required by that upstream v1 API.

All attempts are single requests. Timeout/error responses do not reroute themselves.
Logs must distinguish backend responses from gateway failures; no prompt, token,
API key, authorization header or response body logging is required.

## Operations and evidence

### Unified deployment names, 2026-09-15

Both active Foundry accounts now expose `gpt-5.1`, model version `2025-11-13`.
East US 2 reuses its existing GlobalStandard deployment (capacity 150); there
were no `svhwb*` deployments in that account. Sweden's replacement was created
alongside `svhwb107-gpt51` with the same GlobalStandard capacity 10,
`Microsoft.DefaultV2` content policy and `OnceNewDefaultVersionAvailable`
upgrade setting. The old Sweden deployment was deleted only after the new one
served direct identity probes and APIM requests. Other models/accounts and
existing RBAC were not changed.

The config, live Logic App probe URLs and both log-alert queries now use
`gpt-5.1`. The seven wildcard APIM operations remain unchanged: this is a
Foundry deployment migration, not restoration of an alias or path rewrite.
Clients must replace any remaining `svhwb107-gpt51` URL or body model value
with `gpt-5.1`; the old deployment no longer exists.

At 12:28 UTC native chat, SSE, v1 chat and Responses all succeeded through
Sweden using `gpt-5.1`. Each backend passed two direct UAMI health probes.
Both alerts and `switchEnabled=true` were restored after access run
`08584121315744907698029855624CU44`. The route remains Sweden-only, version 2;
East US 2 was not re-enabled, and this was not a new failover drill.

### Pure wildcard cleanup, 2026-09-15

Historical 12:16 observations below precede the deployment-name unification.
Current client examples above use `gpt-5.1` in both regions.

Removed all three `Azure OpenAI ...` operations and their creation code, request
deployment rewrites, embedding selection branch, `llm-embedding` backend and
dedicated embedding configuration/grant-script entry. The deployment script
retains only idempotent deletion of known retired resources so redeployment
cannot recreate them. Unrelated operations/backends are not deleted.

Existing Foundry accounts/model deployments and previously granted RBAC are not
deleted or revoked by this proxy cleanup. The shared model UAMI and controller
remain necessary. Historical executor-retirement records are snapshots, not
current endpoint instructions.

At 12:16 UTC the pure proxy returned 200/OK for the actual Sweden deployment
(3901ms), SSE with four chunks and `[DONE]` (2273ms), v1 chat (2963ms) and
Responses API (2497ms, `completed`, `store=false`). Invalid query/body requests
returned backend 404/400. All seven HTTP methods on a nonexistent nested path
returned backend 404 and one attempt. No real upstream files/jobs were modified.

The previously aliased `gpt-5.1` deployment path and the former dedicated embedding
path both went unchanged to Sweden and returned backend `404 DeploymentNotFound`.
This is the intentional removal of routing exceptions, not an APIM route miss.
At that time clients had to use the actual `svhwb107-gpt51` deployment instead.
GatewayLogs record all these calls through `proxy-post` / other wildcard IDs.
An additional deployment did not recreate the retired operations/backend.
Both alert rules and `switchEnabled` were restored; access run
`08584121321001264721224949538CU00` succeeded without changing route content.

Chat alert attribution includes POST requests to the configured deployment chat
URLs and both origins' `/openai/v1/chat/completions` and `/openai/v1/responses`.
It does not count arbitrary wildcard paths, file/job operations or embeddings as
chat health. All forwarded paths still produce GatewayLogs. Existing thresholds,
quarantine and no-retry behavior remain unchanged.

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
