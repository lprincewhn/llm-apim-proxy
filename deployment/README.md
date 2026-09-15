# Deploying monitored APIM routing

The current design is documented in [Chinese](../docs/monitoring-failover.zh-CN.md).
The business API forwards once; Azure Monitor changes the route for future
requests through a separate Logic App. The executor remains a real dependency
of both the business API and the controller's model probes.

## Lab resources

| Resource | Name / purpose |
|---|---|
| Subscription | `10564893-ecc3-4a6d-b505-53bcbe89dd8e` (MCAPS) |
| Resource group | `rg-svhwb107-apim-lab` |
| Developer APIM | `apim-svhwb107-0915` |
| Gateway | `https://apim-svhwb107-0915.azure-api.net` |
| Container App / environment | `exec-svhwb107` / `cae-svhwb107` |
| ACR | `acrsvhwb1070915.azurecr.io` |
| Log Analytics | `law-svhwb107-0915` |
| Executor identity | `id-svhwb107-exec`, principal `8f1e45f4-0ac1-400d-b403-87ab38dac147` |
| Logic App / Action Group | `llm-monitored-failover` |
| Controller identity principal | `f11678b8-01cb-4b24-a17f-584925f02b65` |
| Log alerts | `llm-backend-latency`, `llm-backend-errors` |

Reused models: `svhw-openai-eastus2/gpt-5.1`,
`svhw2-swedencentral/svhwb107-gpt51`, and
`svhw2-westus3/text-embedding-3-small`. The Sweden deployment was created in the
existing account for this lab. GlobalStandard does not guarantee inference
physically stays within the resource's region.

## Deployment order and side effects

These scripts target **existing MCAPS lab prerequisites**, not an arbitrary
subscription. They do not create every resource in the table. No credentials are
stored in the repo. All Azure helpers explicitly select the subscription.

1. Build/publish the executor image using its [Docker instructions](executor/README.md).
   `deploy_executor.py` references `executor:v1`; choose the intended immutable
   image before publishing a production revision.
2. `python3 deployment/deploy_executor.py` is an initial deployment script.
   It rotates the executor key and the ACR token password; running it on an
   active system requires coordinated APIM and Logic App parameter refresh.
   Do not run it merely to update documentation or recover a route.
3. `bash deployment/grant-required-roles.sh` is an administrator action granting
   the executor model access, not controller management access.
4. `python3 deployment/configure_apim.py` configures `/llm`, subscription,
   Named Value secret references and diagnostics, **preserving existing
   `chat-route` by default**. It fails if the prerequisite route is missing.
   `--initialize-route` explicitly creates/resets East US 2 primary and both
   enabled; use only for deliberate initialization, never routine recovery.
5. Follow [monitoring deployment and activation](monitoring/README.md):
   deploy disabled, grant controller Named Value RBAC, verify access and probes,
   then enable alerts and perform an actual alert-to-new-request drill.

`llm-policy.xml` is generated from `configure_apim.build_policy()` and contains no
secrets. For offline regeneration, import that function and write its returned XML;
importing the module performs no Azure operations.

The existing APIM subscription ID `lab-validation` is retained for client
compatibility. It is not a validation API. No request-retry demo API is generated.
This source cleanup does not remove any previously deployed legacy API or image.

The ACR pull token is repository-read-only and expires 30 days after generation;
rotate it deliberately if retaining the lab. Warm Container Apps, Developer APIM,
ACR, logs, Logic Apps and model requests continue to incur charges.

## Business API contract

All endpoints are POST and require `Ocp-Apim-Subscription-Key`. Obtain the key
through the authorized APIM subscription interface, never source files.

| Path | Logical model | Request budget |
|---|---|---|
| `/llm/intent` | `gpt-5.1` | 5000ms |
| `/llm/rewrite` | `gpt-5.1` | 1200ms |
| `/llm/generate` | `gpt-5.1` | 4000ms |
| `/llm/embedding` | `text-embedding-3-small` | 500ms |

`X-Remaining-Budget-Ms` may reduce, not increase, the budget. APIM reserves
250ms for forwarding/return and rounds its outer timeout to seconds; the executor
enforces the fine-grained budget. These are limits, not measured model SLAs.
Non-streaming only; the caller still owns its overall business deadline.

Chat uses the current `chat-route.primary`; embedding has one independent
backend and is excluded from chat failover alerts. `X-Lab-Backend` and
`X-Lab-Attempts` report the selected backend and single attempt on normal responses.
Errors are returned, not rerouted in place. Backend paths and response codes
feed resource-specific gateway logs without collecting prompt/response bodies.

## Last observed operational state

The [2026-09-15 drill](monitoring/drill-20260915.md) passed with a real error alert
and new Sweden business completion. Both alerts are enabled and
`switchEnabled=true`; route version 2 has `primary=sweden`, `enabled=[sweden]`.
East US 2 was quarantined by controlled caller-budget timeouts, not an observed
Foundry outage. No backup is enabled until an operator validates/re-enables it.
Use `python3 deployment/monitoring/deploy.py status` for current controller state.

Do not automatically fail back or reset routes when redeploying. ETag-protected
operator recovery must preserve unrelated route fields and account for active
workflow runs. Disable commands stop new alert work, not an already running write.

## Acceptance boundary

Keep the [actual closed-loop evidence](monitoring/drill-20260915.json), not legacy
synthetic retry results. A real alert, two model probes and a successful new
gateway request prove this control loop only. Latency-trigger-specific drills,
caller-versus-backend error classification, N-1 capacity, production HA/network
hardening and Java/Search 8s/15s performance remain separate.

Deleting repository files is not permission to delete cloud resources. Never
delete the existing Foundry accounts, their resource group, or original APIM.
