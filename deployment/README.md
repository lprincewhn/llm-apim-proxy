# SVHWB-107 Azure deployment handoff

## Status

The isolated APIM and buffered executor are deployed. As of 2026-09-15 08:50 UTC,
real East US 2/Sweden GPT-5.1 and West US 3 embedding probes succeeded; the
executor's model RBAC blocker is resolved.

The corrected design is **APIM telemetry -> Azure Monitor -> Action Group ->
Logic App -> backup health check -> APIM route update -> future requests**.
The `/llm` API no longer retries or switches endpoints within a request.
See `monitoring/README.md` for deployment, dedicated controller RBAC and activation.
The current deployment principal cannot create role assignments. Do not disable
Foundry `disableLocalAuth` or copy privileged credentials into the controller.

This is a Developer-tier lab, not a production release. The Java orchestration,
Azure AI Search and 8-second knowledge-base/15-second full-turn integration are
not deployed in this package.

## Resources

- Subscription: `10564893-ecc3-4a6d-b505-53bcbe89dd8e`
- Resource group: `rg-svhwb107-apim-lab`
- APIM: `apim-svhwb107-0915` (Developer, East US 2)
- Gateway: `https://apim-svhwb107-0915.azure-api.net`
- Executor: `exec-svhwb107` (Container Apps, 0.5 CPU/1 GiB, min 1/max 2 replicas)
- Environment: `cae-svhwb107`
- Registry: `acrsvhwb1070915.azurecr.io`, image `executor:v1`
- Log Analytics: `law-svhwb107-0915`
- Latency alert: `alert-svhwb107-backend-latency`
- Identity: `id-svhwb107-exec`
- Identity principal: `8f1e45f4-0ac1-400d-b403-87ab38dac147`
- Identity client ID: `3dfb189c-3492-4eb9-80f8-4bc97213bf76`

The existing `svhw-openai-eastus2/gpt-5.1` and
`svhw2-westus3/text-embedding-3-small` deployments are reused unchanged.
One new, isolated model deployment was created inside the existing
`svhw2-swedencentral` Foundry account: `svhwb107-gpt51`, GPT-5.1 version
`2025-11-13`, GlobalStandard capacity 10. No Canada East Foundry account was
found in this subscription. These GlobalStandard endpoints do not establish
independent region-local inference guarantees.

An App Service B1 plan attempt was rejected because the regional quota is zero;
no App Service instance remains. Container Apps is used instead.

## Executor authorization (completed)

Run `grant-required-roles.sh` as an authorized Owner/User Access Administrator.
It grants only `Cognitive Services OpenAI User`, scoped to each of the three
existing Foundry/OpenAI accounts, to the new executor identity.

Historical upstream response before authorization propagation:

```json
{"status":401,"code":"PermissionDenied","message":"Principal does not have access to API/Operation."}
```

This model authorization is separate from the new controller's APIM management
authorization. `monitoring/` documents the latter. Do not treat historical
401 latency as model completion latency. Probes use synthetic, non-sensitive text.

## Client interface

All POST endpoints require `Ocp-Apim-Subscription-Key`. Obtain the key from
APIM Subscriptions -> `SVHWB107 validation client` in the portal. Keys are not
included in this archive or issue comments.

| Endpoint | Body | Stage limit |
|---|---|---|
| `/llm/intent` | Chat Completions JSON, logical model `gpt-5.1` | 5000 ms |
| `/llm/rewrite` | Chat Completions JSON, logical model `gpt-5.1` | 1200 ms; no retry |
| `/llm/generate` | Chat Completions JSON, logical model `gpt-5.1` | 4000 ms |
| `/llm/embedding` | Embeddings JSON, model `text-embedding-3-small` | 500 ms; no retry |

`X-Remaining-Budget-Ms` can reduce the stage budget but cannot increase it.
Callers must still enforce their whole-turn deadline. Non-streaming only.
APIM translates logical model names to the selected physical deployment.

The gateway forwards to a fully buffering executor, which receives a bounded
budget reduced by a 250 ms forwarding/return allowance. APIM's outer
`forward-request timeout` is rounded to seconds because the deployed gateway
rejected a `timeout-ms` expression above 1000. Executor and caller deadlines,
not this rounded outer timeout alone, enforce fine-grained total-call bounds.

Every business request is forwarded once, to the route's current primary
(embedding remains separate). Errors/timeouts are returned to the caller;
only a subsequent control-plane update affects later requests. The initial
logical route is primary `eastus2`, enabled `eastus2,sweden`. The controller
quarantines the failed primary and does not automatically fail back.

## Fault validation

`/validation/{scenario}` is a separate subscription-protected API, forwarding
to the executor's loopback synthetic upstream. It never invokes Foundry.
`body-stall` sends a real HTTP 200 header and partial body before pausing.
The executor turns its body-read failure into a controlled 504; APIM retries
the synthetic success path and reports `X-Lab-Attempts: 2`.

Headers `X-Lab-Backend` on these synthetic calls are **routing labels**, not
evidence of successful inference from that geographic region.

`validation-results.json` and `extended-results.json` contain actual observed
results: 17 checks passed, 7 live model integration checks failed. Executor
unit suite: 40 passed; local routing-state suite: 4 passed.

The extended test explicitly changes the live lab route to Sweden and restores
East US 2, confirming gateway configuration propagation. **This is not proof
of an Azure Monitor alert automatically changing the route.**

## Monitoring-driven routing

Gateway log ingestion was confirmed on 2026-09-15. `ApiManagementGatewayLogs`
contains `ApiId`, `BackendUrl`, `BackendTime` (milliseconds), and response codes.
For `/llm`, `/execute/eastus2` and `/execute/sweden` identify the selected backend.
Single-attempt business policies avoid final-attempt-only attribution ambiguity.
Request/response bodies and credential headers are not logged.

The original aggregate `BackendDuration` metric alert is auxiliary and has no
actions. The new per-backend log alerts and Action Group are defined in
`monitoring/`, excluding `/validation` and embedding. Log ingestion, alert
evaluation, workflow execution and gateway configuration propagation all add
delay: this is neither a per-request deadline mechanism nor an instantaneous switch.

`routing.py` remains an early reference only. The Logic App uses real backup
completion probes, serialized execution, ETag conditional writes, cooldown,
stale-alert rejection and explicit quarantine. Two successful short probes do
not prove N-1 capacity. Dedicated MI authorization was verified on 2026-09-15;
both alerts are now enabled. A real error-alert-to-new-gateway-request drill
switched East US 2 to Sweden successfully. See `monitoring/drill-20260915.md`.
Latency-trigger-specific and full business performance acceptance remain separate.

## Reproduction and operational caution

`azure.py` keeps ARM tokens and secret values in memory. Deployment scripts
require an already-authorized Azure CLI session and always specify MCAPS.
Scripts are for this isolated lab, not general production IaC.

`configure_apim.py` resets route configuration to the initial East US 2 primary.
Use `use_monitoring_routing.py` instead when migrating the existing business
policy: it removes retry, preserves Named Values, and leaves `/validation` alone.
`deploy_executor.py` is an initial deployment script: it generates an executor
secret and registry pull credential; rerunning it requires updating APIM named
values with `configure_apim.py` and waiting for propagation. For rolling
production updates use versioned secrets rather than this lab script.

The ACR credential is a repository-read-only token, not an admin credential,
and expires 30 days after creation. Rotate it before expiry if retaining this lab.
The managed identity uses Azure OpenAI RBAC, not this registry token.

The executor does not need a process on the agent machine. Azure resources
remain running and may incur charges: Developer APIM, warm Container Apps,
ACR, log ingestion/storage, and any future model usage.

For cleanup, an authorized operator can delete **only**
`rg-svhwb107-apim-lab` and the separately created
`svhw2-swedencentral/svhwb107-gpt51` model deployment. Never delete the existing
Foundry resource group/accounts or other model deployments. Remove any added
role assignments for this identity if they were subsequently granted.

Production work still requires a production APIM SKU, high availability,
network hardening, end-to-end Java/RAG deadlines, performance/quality tests,
N-1 capacity, and a proven monitoring-controller loop.
