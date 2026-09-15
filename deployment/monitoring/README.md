# Direct Foundry monitoring controller

The controller implements **APIM gateway logs → Azure Monitor → Action Group →
Logic App direct backup probes → conditional APIM route write → future requests**.
There is no executor, executor key, Container App endpoint or intermediary probe.
See the [Chinese design](../../docs/monitoring-failover.zh-CN.md).

## Identity and endpoint contract

The Logic App has two identities:

- Its system-assigned identity accesses ARM `namedValues/chat-route` with
  `https://management.azure.com/` audience. This is unchanged from the accepted
  original controller.
- Existing user-assigned identity `id-svhwb107-exec` calls Foundry with
  `https://cognitiveservices.azure.com` audience. The resource ID is explicitly
  selected on model HTTP actions. Its historical name does not imply an executor.
  The same UAMI is attached to APIM and already has model grants.

`deployment/backends.py` validates `config.json` and generates direct completion
URLs for `eastus2` and `sweden`. The workflow's `backendUrls` object comes from
this configuration, never an event-supplied endpoint. APIM uses only the two
origins; deployment names are probe/log-attribution configuration, not request
rewrites. No API key, secure executor parameter or service-principal credential
is embedded.

## Alerts

Two scheduled-query rules evaluate a five-minute window every minute:

| Name | Condition, with at least five eligible requests |
|---|---|
| `llm-backend-latency` | Average `BackendTime >= 3200` milliseconds |
| `llm-backend-errors` | HTTP 429 / 500–599 rate `>= 20%` |

Queries filter `_ResourceId` and `ApiId='llm'`, strip the query string from
`BackendUrl`, then match the **complete configured HTTPS origin and deployment
operation path**: configured deployment completions plus each origin's
`/openai/v1/chat/completions` and `/openai/v1/responses`, POST only.
Similar hosts, other deployments, arbitrary wildcard file/job paths, legacy `/execute/*` and
embedding are excluded. `BackendId` is useful for diagnostics but not required
by the query. This is endpoint attribution, not proof of a regional root cause.

The public API is a root wildcard HTTP proxy; ARM `ApiId='llm'` remains unchanged.
Only seven wildcard operations exist; all paths are forwarded unchanged with
no deployment aliases or fixed embedding target. Query `api-version` is forwarded, not replaced, and
is excluded from attribution. Both non-streaming and SSE chat calls enter the
same population; a post-header stream interruption is not necessarily a 5xx,
and these rules do not measure SSE idle time or guarantee detection of every
interrupted stream.

Errors count either gateway `ResponseCode` or `BackendResponseCode`. A 401/403 in
either field excludes that sample from both health-alert populations, rather
than initiating failover for an authorization configuration problem. Such errors
need separate operational attention. Missing backend responses may still be
counted through the gateway's 5xx status. No prompt/answer logging is needed.

## Failover workflow

1. Validate common alert schema, exact rule name, workspace target and a single
   allowed Backend dimension. Only `Fired` is eligible; `Resolved` is a no-op.
2. Reject events older than ten minutes or future-dated events. Execute one run
   at a time, bounded queue; read route plus concrete ETag using system identity.
3. Require alerted backend equals current primary, both primary and other backend
   are enabled, and ten-minute cooldown has elapsed.
4. Directly call the other Foundry deployment twice, sequentially: `Reply only OK`,
   `max_completion_tokens=32`, `reasoning_effort=none`, `stream=false`.
5. Each response must be HTTP 200, exactly one choice, string content exactly
   `OK`, and `finish_reason=stop`. Real model failures/timeouts do not qualify.
6. Recheck freshness, then send `If-Match` PUT preserving unrelated route fields;
   set primary to backup, enabled to only backup, increment version and record
   timestamp/reason. Require completed write and exact ARM read-back.

Every HTTP action disables retries and asynchronous 202 polling. Removing the
executor removes its 5500ms total/1200ms idle controls. **Consumption synchronous
HTTP can wait up to the platform's 120-second limit per request**, not 5.5 seconds
or an eight-second SLA. Probe failure still cannot result in a successful switch.

ETag conflict / denied access / failed probes fail closed with no fallback.
There is no automatic rollback: an error after PUT may leave a committed switch.
Read-back is control-plane evidence only; verify gateway propagation separately
with a new real business request.

Quarantine is sticky; no automatic failback. An operator must validate/re-enable
the removed target. If only one backend is enabled, no automatic standby exists.

## Deploy and verify

Run from the repository root. Python uses only the standard library; offline
commands do not contact Azure:

```bash
python3 deployment/monitoring/deploy.py template
python3 -m unittest discover -s deployment/monitoring -p 'test_*.py' -v
```

Prepare existing resource prerequisites and APIM direct routing as described in
the [deployment handoff](../README.md), then:

```bash
# Validates the ARM template and creates/updates resources.
# Every deployment returns to disabled alerts and switchEnabled=false.
python3 deployment/monitoring/deploy.py deploy
python3 deployment/monitoring/deploy.py status
```

Resources: Consumption Logic App and Action Group `llm-monitored-failover`,
rules `llm-backend-latency` and `llm-backend-errors`. Query validation is enabled.
The Action Group uses the common schema and a SAS-authorized Logic App callback;
callback values remain in memory/ARM, not CLI output or artifacts.

If permissions are missing, an RBAC administrator runs:

```bash
bash deployment/grant-model-roles.sh
bash deployment/monitoring/grant-route-role.sh
```

The first script grants model calls to the shared UAMI at the two current model account
scopes. The second grants `API Management Service Contributor` only at the
existing `chat-route` Named Value to the workflow's **system** identity. The
deployer does not grant roles or supply a privileged credentials fallback.

With both alerts disabled:

```bash
python3 deployment/monitoring/deploy.py verify-access
python3 deployment/monitoring/deploy.py health-check --backend eastus2
python3 deployment/monitoring/deploy.py health-check --backend sweden
```

`verify-access` performs a real system-MI GET and ETag-protected **unchanged-value
PUT**; this may cause control-plane propagation but does not change route content.

`health-check` sends an explicit `monitoring.healthCheck.v1` maintenance event,
checks two real direct completions via the UAMI, and verifies action statuses.
The CLI requires alerts disabled and `switchEnabled=false`; the workflow also
enforces `switchEnabled=false`. Its maintenance branch has **no route read/write
actions**. Checking a quarantined endpoint does not re-enable it.

The existing `probe-only --event-file <file>` command instead exercises the
**automatic alert decision path**, including current-primary/backup eligibility.
Supply a current common-schema Fired event with the configured workspace target,
an allowlisted rule and a single Backend dimension. Success in no-write mode is
Cancelled at `DryRunNoWrite` after both model validation actions succeed. Other
Cancelled outcomes are guards, not proof of backup health.

## Activation and inspection

Confirm actual APIM log records contain direct Foundry `BackendUrl`, expected
API/resource IDs and millisecond timing, and that current alert payload format
matches the accepted common schema. Keep failure samples distinct from pure
model latency; incomplete schema evidence is not a reason to disable query validation.

```bash
# Re-verifies real MI management access before enabling.
python3 deployment/monitoring/deploy.py enable-alerts --confirm-log-schema
python3 deployment/monitoring/deploy.py status

# Stop new dispatch and set probe-only mode.
python3 deployment/monitoring/deploy.py disable
```

An activation error attempts to restore disabled defaults and surfaces failure.
Disabling does not cancel a run already in progress. Inspect action **statuses**:

```bash
python3 deployment/monitoring/deploy.py inspect-run --run-id <run-id>
# Disabled alerts required; Resolved event must skip probes and all route work.
python3 deployment/monitoring/deploy.py smoke-ignored
```

Avoid logging/debugging callback URLs or raw action bodies. HTTP, compose and
parse histories remain secured; endpoint mappings are non-secret, tokens are not.
Payload validation is not cryptographic evidence of Azure Monitor authorship:
protect SAS callback access. Callback holders can also invoke maintenance
operations; the health branch is restricted to fixed targets and no route writes.

## Evidence and limits

The [09:17 historical drill](drill-20260915.md) used the retired executor. It is
not direct-topology acceptance. See [direct migration record](direct-migration-20260915.md)
for the new APIM completions, direct workflow probes, real logs and retirement.

Preserve `primary=sweden`, `enabled=[sweden]`, version 2 unless an operator
explicitly changes it. Neither a successful probe nor deployment clears quarantine.
One short request or two probes do not prove N-1 capacity, production HA,
the Java/Search chain or 8s/15s full-turn targets.
