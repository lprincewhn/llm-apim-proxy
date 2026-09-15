# Direct Foundry migration — 2026-09-15

Historical migration snapshot, before the later native API update.
The custom operation names and short APIM budgets below have since been retired;
see the current [native API contract](../README.md#api-contract-and-changed-timeout-behavior).

The MCAPS lab now uses `client → APIM → Foundry`, with no model executor.
The asynchronous control path remains `GatewayLogs → Azure Monitor → Action
Group → Logic App direct backup probes → ETag route update → future requests`.
All times are UTC. No credentials, callback URLs or model vectors are recorded.

## Deployed changes

APIM uses `llm-eastus2`, `llm-sweden` and `llm-embedding` named backends,
fixed deployment paths and managed-identity authentication. Each request is
forwarded once. Logic App probes call the fixed Foundry deployments directly.

APIM and Logic App share the already-authorized UAMI `id-svhwb107-exec`;
its historical name does not mean the executor remains. The workflow retains
its system identity for ARM route operations. No new RBAC grant was needed.

Both alert queries now match the complete direct Foundry URL without query
parameters. They exclude legacy executor paths, embedding and any 401/403
sample. Five eligible requests in five minutes are required; average backend
time at least 3200ms or 429/5xx rate at least 20% triggers the corresponding
per-backend rule. Rules evaluate every minute.

## Direct calls and logs

Initial direct smoke at 10:17 returned 200 for all four APIM operations.
After the executor Container App was deleted, the 10:36:22 smoke returned:

| Operation | Backend | Result | Client elapsed |
|---|---|---|---|
| intent | Sweden GPT-5.1 | 200, `OK`, `stop`, one attempt | 2426ms |
| rewrite | Sweden GPT-5.1 | 200, `OK`, `stop`, one attempt | 2508ms |
| generate | Sweden GPT-5.1 | 200, `OK`, `stop`, one attempt | 2445ms |
| embedding | West US 3 | 200, finite 1536-dimensional vector, one attempt | 1553ms |

The preceding 10:35 smoke had one embedding HTTP 500 (1902ms); the other
three operations succeeded. Its gateway record shows `LastErrorReason=Timeout`,
`LastErrorSource=request-forwarder`, `BackendTime=992ms` and no backend response
code: the one-second forwarding limit was reached. This is not proof of a
Foundry outage. The later successful call does not erase that failure or
establish reliability. Embedding is deliberately outside the chat failover rules.

Actual `ApiManagementGatewayLogs` showed `BackendId=llm-sweden` and:

```text
https://svhw2-swedencentral.openai.azure.com/openai/deployments/svhwb107-gpt51/chat/completions?api-version=2024-10-21
```

Three initial chat samples had HTTP 200 and `BackendTime` values 3452, 1609
and 1612ms. The embedding sample had `BackendId=llm-embedding`, a direct
West US 3 embeddings URL, HTTP 200 and `BackendTime=669ms`.

Post-retirement 10:36 logs independently show the same direct named backends:
chat backend times 1520/1612/1526ms and embedding 457ms, all HTTP 200.

Executing the revised error-query aggregation with an observation-only
45-minute window returned Sweden: three samples, zero errors, average
2224.33ms. Embedding was absent as intended. This confirms actual URL
attribution, not a threshold-crossing alert. Deployed windows remain five minutes.

## Direct controller checks

| Operation | Workflow run | Outcome |
|---|---|---|
| East US 2 direct maintenance probes | `08584121390579026625540510577CU36` | Both model probes passed; all route actions skipped |
| Sweden direct maintenance probes | `08584121390480780969766123359CU12` | Both model probes passed; all route actions skipped |
| System-MI route permission check | `08584121390352212657342186270CU38` | GET and unchanged-value conditional PUT succeeded |
| Activation permission recheck | `08584121383436341571981890615CU40` | GET and unchanged-value conditional PUT succeeded |

Maintenance probes require two real completions, each HTTP 200, exact `OK`
and `finish_reason=stop`. They neither clear quarantine nor simulate a Monitor
alert. Access checks do not change route content.

Both `llm-backend-latency` and `llm-backend-errors` were restored to enabled,
and `switchEnabled=true`, after direct model, identity and log-schema checks.
The Action Group continues to use the common alert schema.

## Retired components

The dedicated executor dependencies were removed after confirming direct callers
and excluding other environment/registry consumers:

| Removed cloud resource/configuration | Name |
|---|---|
| Container App | `exec-svhwb107` |
| Container registry and executor image | `acrsvhwb1070915` |
| Legacy APIM API | `validation` |
| Executor Named Values | `executor-key`, `executor-base` |
| Unconnected legacy aggregate metric alert | `alert-svhwb107-backend-latency` |

The delete command for the now-empty environment `cae-svhwb107` returned
success, but final ARM read-back still reports **`ScheduledForDelete`**.
Azure has accepted its asynchronous deletion; the environment's absence is
not yet confirmed. The Container App and ACR are absent. No local deletion
process is left running. Route content remained unchanged throughout retirement.
Source removal
includes `deployment/executor/`, its Docker/dependency/test files and
`deployment/deploy_executor.py`. Model RBAC instructions are now
`deployment/grant-model-roles.sh`, not executor deployment instructions.

Required retained resources are APIM, Log Analytics, the shared model UAMI,
active Logic App, Action Group and two per-backend query rules. The empty
environment remains visible only pending Azure deletion. Existing Foundry
accounts/deployments were not deleted or recreated.

## Retained route and limitations

```json
{
  "primary": "sweden",
  "enabled": ["sweden"],
  "version": 2,
  "lastSwitchAt": "2026-09-15T09:17:39.0311001Z",
  "reason": "Azure Monitor llm-backend-errors quarantined eastus2"
}
```

East US 2 remains quarantined by the earlier controlled drill. Successful
maintenance probes do not authorize recovery. **There is no enabled backup**
until an operator explicitly restores one; there is no automatic failback.

This migration did not force another route switch or repeat the entire
direct-topology real-alert failover drill. The [09:17 drill](drill-20260915.md)
used the old executor and must not be represented as direct-topology evidence.

Removing the executor also removes its full-body/idle deadlines, explicit JSON
buffer validation and 2MiB cap. APIM forwarding timeouts are rounded to whole
seconds and are not full-body deadlines; direct Consumption Logic App HTTP
probes can wait up to the platform's 120-second synchronous limit. Neither
500ms embedding nor Java/Search 8s/15s targets are proven. Latency-triggered
failover, sustained reliability, N-1 capacity and production HA remain unaccepted.
