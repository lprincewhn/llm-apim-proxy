# Azure Monitor–driven APIM failover

This controller is **off by default**. It is not a gateway retry policy:

1. APIM records one business attempt in `ApiManagementGatewayLogs`.
2. Two Azure Monitor scheduled-query alerts evaluate each backend every minute,
   over the preceding five minutes, with at least five requests:
   - Average `BackendTime >= 3200` milliseconds.
   - HTTP 429 / 500–599 error rate `>= 20%`.
3. An Action Group delivers the common alert schema to a Consumption Logic App.
4. The workflow reads the **existing** APIM `namedValues/chat-route`, verifies the
   alert concerns the current primary, and checks that the other fixed backend
   (`eastus2` or `sweden`) is explicitly enabled.
5. Two **sequential real GPT requests** to the executor must pass before the
   workflow's system-assigned identity changes that named value with ARM.
6. Future gateway requests can use the new primary after APIM propagates the
   control-plane update. This workflow does **not** prove gateway propagation.

## Current lab deployment

On 2026-09-15, Azure validated and deployed the Logic App, Action Group and both
disabled query rules. APIM gateway logs were confirmed ingested. The business
policy has no retry; a deliberately reduced request budget returned 504 with
one attempt rather than moving to another backend.

The controller's dedicated principal is
`f11678b8-01cb-4b24-a17f-584925f02b65`. Its access-check run
`08584121436710629255686979683CU36` failed at `Read_access_route` with
`Forbidden`. No route write ran. Both alerts remain disabled and
`switchEnabled=false`, pending the administrator grant below. This is a new
**APIM management** permission, separate from the executor's already-working
Foundry inference roles. The full live alert-to-gateway-switch drill is blocked,
not passed.

Only `deployment/monitoring/` is owned by this implementation. The executor,
APIM policy, diagnostics configuration, and initial route remain prerequisites.
No additional Python packages are needed.

## Lab deployment checkpoint — 2026-09-15

The ARM deployment `llm-monitoring` succeeded. Read-back confirmed the Logic App
is enabled with `switchEnabled=false`, and **both alerts are disabled**. Its
system-assigned identity principal is
`f11678b8-01cb-4b24-a17f-584925f02b65`.

APIM log ingestion was confirmed: `ApiId='llm'`, executor paths in `BackendUrl`,
numeric millisecond `BackendTime`, `ResponseCode`, and `BackendResponseCode`.
Scheduled-query creation passed query validation. This does not verify actual
common-alert payload delivery or real alert-to-GPT-to-ARM switching.

The Contributor deployment identity cannot perform
`Microsoft.Authorization/*/Write`. **No role grant or live switching run was
performed.** An RBAC administrator can use the script below or this concrete
single-resource command:

```bash
az role assignment create \
  --subscription 10564893-ecc3-4a6d-b505-53bcbe89dd8e \
  --assignee-object-id f11678b8-01cb-4b24-a17f-584925f02b65 \
  --assignee-principal-type ServicePrincipal \
  --role 312a565d-c81f-4fd8-895a-4e21e48d571c \
  --scope '/subscriptions/10564893-ecc3-4a6d-b505-53bcbe89dd8e/resourceGroups/rg-svhwb107-apim-lab/providers/Microsoft.ApiManagement/service/apim-svhwb107-0915/namedValues/chat-route' \
  --only-show-errors -o none
```

After the grant, perform the explicit MI verification and probe-only checks
below; do not enable alerts merely because the assignment command succeeded.

Before the grant, the ignored-Resolved smoke run
`08584121435153607872931604082CU32` completed **Cancelled**, with
`ResolvedNoOp: Succeeded`; all workflow ARM read/write and model probe actions
were **Skipped**. This verifies the no-op branch without requiring MI access or
changing the route. It is not live failover validation.

## Safety contract

- Each query restricts `_ResourceId` to this APIM and `ApiId` to `llm`, derives
  `Backend` from `/execute/eastus2` or `/execute/sweden` in `BackendUrl`, and
  produces a per-backend `ViolationCount` of zero or one.
- The trigger accepts only the common schema, the two exact rule names, exactly
  one workspace target, exactly one condition, and exactly one `Backend`
  dimension whose value is `eastus2` or `sweden`. An invalid body fails closed.
- Only `Fired` alerts can switch. `Resolved` is a cancelled no-op, not failback.
  Events older than ten minutes or future-dated events cannot switch. Freshness
  is checked again after probes.
- Trigger concurrency is one run, with a bounded waiting queue. A ten-minute
  cooldown uses the route's `lastSwitchAt`. A missing timestamp means no previous
  switch; an invalid timestamp fails closed.
- Each probe calls the existing executor `/execute/{backup}`, not `/health`:
  `Reply only OK`, `max_completion_tokens: 32`, `reasoning_effort: none`,
  `stream: false`, `X-Budget-Ms: 5500`, and `X-Idle-Ms: 1200`.
- Both probes require HTTP **200**, exactly one choice, string
  `message.content == "OK"`, and `finish_reason == "stop"`. Refusal, truncation,
  empty text, malformed JSON, synthetic health JSON, timeout, and any failed
  HTTP action fail the run without a route write. There is no success-shaped
  fallback. Whitespace or commentary around `OK` deliberately fails too.
- All HTTP actions disable retries and the asynchronous 202 polling pattern.
  **Consumption synchronous HTTP has a platform-bound 120-second transport
  timeout**; there is no claim that an action-level timeout reduces it to 5.5
  seconds. The executor enforces its 5500 ms total / 1200 ms idle budgets after
  receipt. A network stall can therefore delay failure longer than the model
  budget, but cannot qualify an unverified backend.
- ARM GET/PUT uses only the workflow's system-assigned identity and only the
  existing `namedValues/chat-route` resource. A concrete GET response ETag is
  required; PUT sends `If-Match`. A concurrent edit / HTTP 412 fails with no retry.
- The route JSON preserves unrelated fields, changes `primary` to the backup,
  changes `enabled` to **only `[backup]`** to quarantine the failed primary,
  increments numeric `version`, and records `lastSwitchAt` and `reason`. Named
  value properties such as display name and tags are also preserved.
- PUT must return 200/201 and a subsequent ARM GET must match the written value.
  A 202 response or read-back mismatch fails rather than claiming completion.
  An accepted write may nevertheless already have changed control-plane state:
  inspect it before retrying.
- No automatic recovery or failback occurs. An operator must independently
  validate and re-enable a quarantined backend. If no backup is enabled, the
  workflow cannot offer availability by inventing one.
- The workflow key is a secureString; trigger, HTTP, compose and parse action
  input/output histories are secured. There are no secret deployment outputs.
  Compose/Parse JSON use `secureData.properties: ["inputs"]`, which also hides
  their outputs; these action types reject an explicit `"outputs"` setting.
  CLI helpers keep callback URLs, tokens and executor keys in memory and do not
  print ARM bodies or exception bodies. Do not use `az --debug`, dump workflow
  parameters/action bodies, or print Action Group receiver definitions.

## Deploy disabled first

Run these from the repository root. All Azure commands below are **operator
steps**, not actions performed by the offline test suite.

```bash
# Offline generators / tests; neither command contacts Azure.
python3 deployment/monitoring/deploy.py template
python3 -m unittest discover -s deployment/monitoring -p 'test_*.py' -v

# Uses the explicit MCAPS subscription in deployment/azure.py.
# Reads the existing executor ingress and executor-key into memory.
# Calls ARM deployments/validate BEFORE creating/updating any resource.
python3 deployment/monitoring/deploy.py deploy
python3 deployment/monitoring/deploy.py status
```

Deployment creates:

| Resource | Name | Initial state |
|---|---|---|
| Consumption Logic App | `llm-monitored-failover` | Enabled, **switchEnabled=false** |
| Action Group | `llm-monitored-failover` | Common-schema Logic App receiver |
| Scheduled query rule | `llm-backend-latency` | **Disabled** |
| Scheduled query rule | `llm-backend-errors` | **Disabled** |

`deploy` always reinstates these safe defaults, including on redeployment.
Its ARM template computes the callback URL with `listCallbackUrl`; the CLI never
writes it to an artifact. Deployment has a bounded foreground provisioning wait;
a timeout means inspect status, not blindly repeat or enable alerts.

The deployment identity is **Contributor only**. This deployer does **not**
create role assignments and never delegates service-principal credentials to
the Logic App. A newly created workflow normally cannot read or write the route.
Keep both alerts disabled until the separate identity grant and verification
below succeed.

## Explicit administrator RBAC handoff

An RBAC administrator, not the Contributor deployment identity, runs:

```bash
bash deployment/monitoring/grant-route-role.sh
```

The script uses the confirmed built-in **API Management Service Contributor**
role ID `312a565d-c81f-4fd8-895a-4e21e48d571c` and resolves the deployed Logic
App's system-assigned principal ID. It grants only:

```text
/subscriptions/10564893-ecc3-4a6d-b505-53bcbe89dd8e/resourceGroups/rg-svhwb107-apim-lab/providers/Microsoft.ApiManagement/service/apim-svhwb107-0915/namedValues/chat-route
```

There is no subscription-, resource-group-, or APIM-service-wide assignment,
no `Owner`/`Contributor` escalation, and no role assignment performed by Python.
The built-in role is broader in operations than a custom read/write role but
its assignment is scoped to this single named value. This dedicated system MI
must not also receive broader roles.

Then run:

```bash
python3 deployment/monitoring/deploy.py verify-access
```

This requires both alerts to be disabled, resets `switchEnabled=false`, invokes
the workflow's explicit `monitoring.accessCheck.v1` maintenance branch, and waits
for that **exact correlated run**. It tests MI GET plus an ETag-guarded PUT of the
**unchanged named-value properties**. This is a real ARM write permission check,
not a role-assignment-list heuristic. It can trigger APIM control-plane
propagation even though the JSON value is unchanged. It is never counted as a
GPT health check. Missing access / 403 / ETag conflict results in a failed run
and alerts stay disabled. Retry explicitly after an administrator resolves RBAC
propagation; do not supply SP credentials.

## No-write smoke and safe run inspection, before the MI grant

These commands do not require the workflow identity to access APIM:

```bash
# Requires disabled alerts and switchEnabled=false. Sends a valid common-schema
# Resolved event; verifies its exact correlated run and all action-status pages.
python3 deployment/monitoring/deploy.py smoke-ignored

# Reinspect the known no-op run, or substitute the ID printed by smoke-ignored.
python3 deployment/monitoring/deploy.py inspect-run \
  --run-id 08584121435153607872931604082CU32
```

The smoke command succeeds only when the run is **Cancelled**, `ResolvedNoOp`
was executed, and every ARM read/write and model probe action was absent or
**Skipped**. It does not read the route through the MI, does not perform the
access-check PUT, and does not call GPT. It only demonstrates ignored-event
handling. `inspect-run` prints action names/statuses, never action bodies,
input/output links, callback URLs, keys, or pagination tokens.

## Probe-only test and activation

Before activation, confirm against actual resource-specific APIM logs:

- The `ApiManagementGatewayLogs` table exists and is receiving this API's data.
- `_ResourceId`, `ApiId`, `BackendUrl`, `BackendTime`, and `ResponseCode` exist;
  `BackendTime` is milliseconds, the selected API ID is actually `llm`, and
  backend URLs contain the selected `/execute/{backend}` path.
- One business attempt produces the intended one sample; diagnostics sampling
  and log ingestion delay are understood. The parent policy must remove retries.
- The actual common-schema log-alert payload has the expected workspace
  `alertTargetIDs`, exact rule name, and single `Backend` dimension. No resource
  ID column is configured: alerts intentionally target the workspace.

`skipQueryValidation` is false. Missing tables/fields can block even a disabled
rule's deployment. Do not bypass this with an assumed-success query.

To exercise the real model probes without changing the route, create a fresh
local common-schema event (no secrets) or use a sanitized captured alert:

```json
{
  "schemaId": "azureMonitorCommonAlertSchema",
  "data": {
    "essentials": {
      "alertRule": "llm-backend-latency",
      "monitorCondition": "Fired",
      "alertTargetIDs": [
        "/subscriptions/10564893-ecc3-4a6d-b505-53bcbe89dd8e/resourceGroups/rg-svhwb107-apim-lab/providers/Microsoft.OperationalInsights/workspaces/law-svhwb107-0915"
      ],
      "firedDateTime": "<current UTC RFC3339 timestamp>"
    },
    "alertContext": {
      "condition": {
        "allOf": [{"dimensions": [{"name": "Backend", "value": "eastus2"}]}]
      }
    }
  }
}
```

Use the **current primary** for the dimension; both primary and backup must
already be in the existing route's `enabled` array.

```bash
python3 deployment/monitoring/deploy.py probe-only --event-file ./event.json
python3 deployment/monitoring/deploy.py status
```

A safe complete preview is **Cancelled at `DryRunNoWrite` after both
`Validate_probe_*` actions succeed**. Other Cancelled branches mean no-op /
rejected alert, not a verified healthy model. An HTTP, permission, JSON, or
model-output failure is a **Failed** run. The status command prints safe run
IDs and states; inspect action **statuses**, not secure input/output bodies, in
the portal for the detailed distinction.

After the real telemetry/payload checks and administrator grant:

```bash
# Repeats the correlated MI read/write verification immediately before enabling.
python3 deployment/monitoring/deploy.py enable-alerts --confirm-log-schema

# Stop new alert dispatch and return the workflow to probe-only mode.
python3 deployment/monitoring/deploy.py disable
```

`enable-alerts` never treats a failed grant verification as success. If enabling
one of the rules fails, it attempts to disable both and restore probe-only mode.
It does not silently retarget or initialize the APIM route.

## Verification boundaries and operations

- Unit tests execute the generated WDL's guard expressions against deterministic
  fixtures and inspect the ARM resource contract. They verify rejected alerts,
  wrong primary, disabled backup, cooldown, genuine response requirements for
  both probes, no-write previews, preservation/quarantine/versioning, ETag
  conflicts, permission denial, and safe deployment/activation gates.
- Tests are **not** the Azure workflow engine or live KQL validation. Cloud ARM
  template validation, actual payload shape, role propagation, and a real
  alert-to-workflow run remain deployment acceptance checks.
- Log ingestion plus a five-minute window / one-minute evaluation is
  **control-plane reaction time**, not request-level 8s/15s latency acceptance.
  Five samples is a configurable design threshold in `alert_query`, not evidence
  that sparse traffic is healthy.
- The callback is SAS-authorized; payload allowlisting is additional validation,
  not cryptographic proof that Azure Monitor authored a request. Protect the
  callback and Action Group read permissions. Possession of the callback permits
  requests to the maintenance branch too, but that branch only writes an
  unchanged route with its ETag.
- A failure after PUT may leave a switched route. Never equate a Failed run
  with guaranteed rollback. No automatic rollback is implemented.
- Disabling is a stop for new work, **not cancellation of an in-flight run**.
  Inspect running executions before manual recovery. ETags protect against
  overwriting a concurrent route edit; they do not undo an already completed
  switch.
- Successful ARM read-back is **not** successful gateway propagation. Separately
  verify the actual next APIM business request's selected backend and completion.
  The Java/Search chain and performance objectives are outside this controller.

References:
[Action Group schema](https://learn.microsoft.com/azure/templates/microsoft.insights/2023-01-01/actiongroups),
[scheduled query rules](https://learn.microsoft.com/azure/templates/microsoft.insights/2023-12-01/scheduledqueryrules),
[common alert schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema),
[Logic Apps limits](https://learn.microsoft.com/azure/logic-apps/logic-apps-limits-and-config).
