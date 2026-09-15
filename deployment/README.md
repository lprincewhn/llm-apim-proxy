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

The existing `lab-validation` subscription ID is retained for client compatibility.
There is no generated validation API. `llm-policy.xml` is an offline-generated
snapshot of `configure_apim.build_policy()`; importing the module has no cloud effects.

Do not delete the model UAMI, Foundry accounts, Log Analytics, APIM or active
controller as part of executor retirement. The old Container App and its dedicated
ACR/environment can be removed only after direct callers are verified and other
consumers are excluded. Exact actions taken are in the migration record.

## API contract and changed timeout behavior

All paths are POST, protected by `Ocp-Apim-Subscription-Key`; get its value through
the authorized APIM interface. Credentials are never stored in source or output.

| Path | Logical model | Configured budget input |
|---|---|---|
| `/llm/intent` | `gpt-5.1` | 5000ms |
| `/llm/rewrite` | `gpt-5.1` | 1200ms |
| `/llm/generate` | `gpt-5.1` | 4000ms |
| `/llm/embedding` | `text-embedding-3-small` | 500ms |

APIM validates the logical model, removes `model`, and targets the fixed deployment
URL using API version `2024-10-21`. Only non-streaming requests are accepted.
Chat follows `chat-route.primary`; embedding remains independent of chat failover.
Client auth and old executor headers are removed before managed-identity auth.

`X-Remaining-Budget-Ms` can shorten the configured budget. APIM calculates remaining
time and **rounds up to whole seconds**, minimum one second. Thus a 500ms input is
not a 500ms hard limit. This is APIM's forwarding timeout, not an executor-enforced
full-body deadline. No explicit complete-JSON validation or executor body-size
cap remains. Caller overall deadlines and cancellation are required.

All attempts are single requests. Timeout/error responses do not reroute themselves.
Logs must distinguish backend responses from gateway failures; no prompt, token,
API key, authorization header or response body logging is required.

## Operations and evidence

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
