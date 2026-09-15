# Primary deployment deletion drill — 2026-09-15

**No automatic switch occurred during 10m05s of observation. No data-plane
failure occurred either, so this run did not measure failure-to-switch time.**

The requested sequence was exercised against the live root-wildcard APIM:
send ongoing native `gpt-5.1` requests, delete the Sweden primary's deployment,
and observe subsequent requests without manually switching primary or invoking
a synthetic alert callback. Existing alert conditions were not changed.

## Preparation and method

The original route was Sweden-only, version 2, so it had no enabled standby.
Both deployments passed two direct UAMI model probes. With alerts disabled and
no active controller runs, an ETag-protected operator update enabled East US 2
as standby, retaining Sweden primary and the old switch timestamp, version 3.
Alerts and automatic switching were then enabled before traffic and deletion.

One sequential request was attempted approximately every five seconds, with
five successful baseline requests. Every request used the same deployment URL,
synthetic `Reply only OK` input and APIM subscription authentication. There were
no request retries, response-cache policies added, or deployment-name rewrites.
Client timestamps, backend headers, request IDs and sanitized results were
recorded; real GatewayLogs independently identified the backend origin/status.

## Observed timeline (UTC)

| Event | Time / result |
|---|---|
| Baseline traffic began | 12:44:20.212 |
| DELETE submitted for Sweden `gpt-5.1` | 12:44:45.514 |
| Azure Activity Log reported DELETE Succeeded | 12:44:47.698 |
| ARM deployment inventory no longer contained `gpt-5.1` | 12:44:48.741 |
| First post-delete completion | 12:44:51.155; Sweden HTTP 200 / OK |
| Last post-delete completion | 12:54:48.712; Sweden HTTP 200 / OK |
| Bounded observation ended | 12:54:50.799; 605.284 seconds after submission |
| Same-name deployment recreated for cleanup | 12:55:02.101; Succeeded |

All **119 post-delete requests** returned HTTP 200 / OK from Sweden, one attempt
each. None reached East US 2. GatewayLogs contain 119 corresponding backend-200
records for the exact Sweden deployment URL, mean backend time **1658.42 ms**,
range **1306–2604 ms**. No workflow run started in the fault-observation window,
and route observations remained Sweden primary, version 3.

Management-plane deletion therefore did **not** establish a data-plane outage
within this window. Delayed data-plane removal is a possible explanation, not
a proven service-internal diagnosis. The result is not “failover takes more
than ten minutes”: there was no observed first failed request from which to
measure that interval.

Separately, the current error rule counts 429 and 5xx, not 404. If deletion
eventually produces `404 DeploymentNotFound`, that alone would not satisfy the
error alert. The latency threshold is 3200 ms average with at least five samples;
observed backend times did not reach it. No alert rules were broadened merely
to force a successful drill.

## Recovery and retained state

The test was bounded to avoid leaving a deleted primary indefinitely. Alerts
were disabled for cleanup; no controller run was active. Sweden `gpt-5.1` was
recreated with its original model/version, GlobalStandard capacity 10, content
policy and upgrade setting. Direct UAMI probes and native/SSE/v1/Responses APIM
requests succeeded afterward.

An ETag-protected operator update restored the **pre-drill East US 2 quarantine**:
`primary=sweden`, `enabled=[sweden]`, version **4**, with the historical
`lastSwitchAt` unchanged. Versions 3 and 4 reflect operator preparation/cleanup,
not automatic failovers. Both alerts and `switchEnabled=true` were restored;
activation access run: `08584121298534350841589514098CU36`.

Raw sanitized observations: `deletion-drill-20260915.json`.
A future timing measurement requires an observed data-plane fault that qualifies
for the configured alerts. This run does not establish current-topology
failover latency, SSE interruption handling or production capacity.
