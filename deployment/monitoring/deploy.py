"""Generate/deploy the disabled, managed-identity monitoring controller.

Importing this module performs no Azure calls. Commands never print ARM bodies,
callback URLs, executor keys, access tokens, or workflow action inputs/outputs.
"""

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from azure import APIM, APP, ROOT, arm, container_secrets  # noqa: E402


LOCATION = "eastus2"
WORKSPACE = ROOT + "/providers/Microsoft.OperationalInsights/workspaces/law-svhwb107-0915"
ROUTE = APIM + "/namedValues/chat-route"
WORKFLOW_NAME = "llm-monitored-failover"
WORKFLOW = ROOT + "/providers/Microsoft.Logic/workflows/" + WORKFLOW_NAME
ACTION_GROUP_NAME = "llm-monitored-failover"
ACTION_GROUP = ROOT + "/providers/Microsoft.Insights/actionGroups/" + ACTION_GROUP_NAME
ALERT_NAMES = ("llm-backend-latency", "llm-backend-errors")
BACKENDS = ("eastus2", "sweden")
LOGIC_API = "2019-05-01"
ALERT_API = "2023-12-01"
ACTION_GROUP_API = "2023-01-01"
DEPLOY_API = "2022-09-01"
SECURE = {"secureData": {"properties": ["inputs", "outputs"]}}


def secured(action):
    action["runtimeConfiguration"] = copy.deepcopy(SECURE)
    if action["type"] in ("Compose", "ParseJson"):
        # These action types reject secureData.outputs; securing inputs also
        # hides their outputs. Downstream HTTP actions are explicitly secured.
        action["runtimeConfiguration"]["secureData"]["properties"] = ["inputs"]
    return action


def after(name):
    return {name: ["Succeeded"]} if name else {}


def http(method, uri, *, previous=None, body=None, headers=None, managed=False):
    inputs = {
        "method": method, "uri": uri,
        "headers": headers or {}, "retryPolicy": {"type": "none"},
    }
    if body is not None:
        inputs["body"] = body
    if managed:
        inputs["authentication"] = {
            "type": "ManagedServiceIdentity",
            "audience": "https://management.azure.com/",
        }
    return secured({
        "type": "Http", "inputs": inputs, "runAfter": after(previous),
        # Do not accept 202 + Location as a successful model response or poll it.
        "operationOptions": "DisableAsyncPattern",
    })


def compose(value, previous=None):
    return secured({"type": "Compose", "inputs": value, "runAfter": after(previous)})


def terminate(status, code, message):
    inputs = {"runStatus": status}
    if status == "Failed":
        inputs["runError"] = {"code": code, "message": message}
    return {"type": "Terminate", "inputs": inputs, "runAfter": {}}


def guard(expression, actions, *, previous=None, code="Rejected", message="Rejected input",
          status="Failed"):
    return {
        "type": "If", "expression": expression, "actions": actions,
        "else": {"actions": {
            code: terminate(status, code, message),
        }},
        "runAfter": after(previous),
    }


def etag(read_action):
    return (
        f"coalesce(outputs('{read_action}')?['headers']?['ETag'], "
        f"outputs('{read_action}')?['headers']?['etag'], "
        f"outputs('{read_action}')?['headers']?['Etag'], '')"
    )


def route_uri():
    return "https://management.azure.com" + ROUTE + "?api-version=2024-05-01"


def probe(previous=None):
    return http(
        "POST", "@concat(parameters('executorBaseUri'), '/execute/', outputs('Backup'))",
        previous=previous,
        headers={
            "Content-Type": "application/json",
            "X-Executor-Key": "@parameters('executorKey')",
            "X-Budget-Ms": "5500",
            "X-Idle-Ms": "1200",
        },
        body={
            "messages": [{"role": "user", "content": "Reply only OK"}],
            "max_completion_tokens": 32, "reasoning_effort": "none", "stream": False,
        },
    )


def probe_schema():
    return {
        "type": "object", "required": ["choices"],
        "properties": {
            "choices": {
                "type": "array", "minItems": 1, "maxItems": 1,
                "items": {
                    "type": "object", "required": ["message", "finish_reason"],
                    "properties": {
                        "message": {
                            "type": "object", "required": ["content"],
                            "properties": {"content": {"type": "string", "enum": ["OK"]}},
                        },
                        "finish_reason": {"type": "string", "enum": ["stop"]},
                    },
                },
            },
        },
    }


def parse(content, schema, previous=None):
    return secured({
        "type": "ParseJson", "inputs": {"content": content, "schema": schema},
        "runAfter": after(previous),
    })


def common_alert_schema():
    return {
        "type": "object", "required": ["schemaId", "data"],
        "properties": {
            "schemaId": {"type": "string", "enum": ["azureMonitorCommonAlertSchema"]},
            "data": {
                "type": "object", "required": ["essentials", "alertContext"],
                "properties": {
                    "essentials": {
                        "type": "object",
                        "required": ["alertRule", "monitorCondition", "alertTargetIDs",
                                     "firedDateTime"],
                        "properties": {
                            "alertRule": {"type": "string", "enum": list(ALERT_NAMES)},
                            "monitorCondition": {
                                "type": "string", "enum": ["Fired", "Resolved"],
                            },
                            "alertTargetIDs": {
                                "type": "array", "minItems": 1, "maxItems": 1,
                                "items": {"type": "string"},
                            },
                            "firedDateTime": {"type": "string"},
                        },
                    },
                    "alertContext": {
                        "type": "object", "required": ["condition"],
                        "properties": {
                            "condition": {
                                "type": "object", "required": ["allOf"],
                                "properties": {
                                    "allOf": {
                                        "type": "array", "minItems": 1, "maxItems": 1,
                                        "items": {
                                            "type": "object", "required": ["dimensions"],
                                            "properties": {
                                                "dimensions": {
                                                    "type": "array",
                                                    "minItems": 1, "maxItems": 1,
                                                    "items": {
                                                        "type": "object",
                                                        "required": ["name", "value"],
                                                        "properties": {
                                                            "name": {
                                                                "type": "string",
                                                                "enum": ["Backend"],
                                                            },
                                                            "value": {
                                                                "type": "string",
                                                                "enum": list(BACKENDS),
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def route_schema():
    return {
        "type": "object", "required": ["primary", "enabled", "version"],
        "properties": {
            "primary": {"type": "string", "enum": list(BACKENDS)},
            "enabled": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "uniqueItems": True,
            },
            "version": {"type": "integer", "minimum": 0},
            "lastSwitchAt": {"type": "string"},
        },
    }


def fresh_expression():
    timestamp = "body('Parse_alert')['data']['essentials']['firedDateTime']"
    return (
        f"@and(greaterOrEquals(ticks({timestamp}), ticks(addMinutes(utcNow(), -10))), "
        f"lessOrEquals(ticks({timestamp}), ticks(utcNow())))"
    )


def updated_route_expression():
    return (
        "@setProperty(setProperty(setProperty(setProperty(setProperty("
        "body('Parse_route'), 'primary', outputs('Backup')), "
        "'enabled', createArray(outputs('Backup'))), "
        "'version', add(body('Parse_route')['version'], 1)), "
        "'lastSwitchAt', utcNow()), "
        "'reason', concat('Azure Monitor ', "
        "body('Parse_alert')['data']['essentials']['alertRule'], "
        "' quarantined ', outputs('Unhealthy_backend')))"
    )


def access_check_actions():
    """An explicit no-value-change MI write, never a model health substitute."""
    read = "Read_access_route"
    write = "Verify_access_write"
    return {
        read: http("GET", route_uri(), managed=True),
        "Access_route_guard": guard(
            f"@and(not(empty({etag(read)})), not(equals({etag(read)}, '*')), "
            f"equals(body('{read}')?['properties']?['secret'], false), "
            f"not(empty(body('{read}')?['properties']?['value'])))",
            {
                write: http(
                    "PUT", route_uri(), managed=True,
                    headers={"Content-Type": "application/json", "If-Match": "@" + etag(read)},
                    body={"properties": "@body('Read_access_route')['properties']"},
                ),
                "Access_write_completed": guard(
                    "@contains(createArray(200, 201), outputs('Verify_access_write')['statusCode'])",
                    {}, previous=write, code="AccessWriteNotCompleted",
                    message="Managed identity write did not complete synchronously; alerts stay disabled.",
                ),
            },
            previous=read, code="AccessRouteUnsafe",
            message="Cannot verify a secret, missing-value, or ETag-less route.",
        ),
    }


def failover_actions():
    # Flat guards avoid Consumption's eight-level action-nesting limit.
    essentials = "body('Parse_alert')['data']['essentials']"
    cooldown = (
        "@lessOrEquals(ticks(if(empty(body('Parse_route')?['lastSwitchAt']), "
        "'1970-01-01T00:00:00Z', body('Parse_route')?['lastSwitchAt'])), "
        "ticks(addMinutes(utcNow(), -10)))"
    )
    return {
        "Parse_alert": parse("@triggerBody()", common_alert_schema()),
        "Allowlisted_workspace": guard(
            f"@equals(toLower({essentials}['alertTargetIDs'][0]), toLower(parameters('workspaceId')))",
            {}, previous="Parse_alert", code="UntrustedWorkspace",
            message="Alert targets a different workspace.",
        ),
        "Fired_only": guard(
            f"@equals({essentials}['monitorCondition'], 'Fired')", {},
            previous="Allowlisted_workspace", status="Cancelled",
            code="ResolvedNoOp", message="No automatic failback.",
        ),
        "Fresh_alert": guard(
            fresh_expression(), {}, previous="Fired_only", status="Cancelled",
            code="StaleAlert", message="Stale or future-dated alert; no route change.",
        ),
        "Unhealthy_backend": compose(
            "@body('Parse_alert')['data']['alertContext']['condition']['allOf'][0]"
            "['dimensions'][0]['value']", previous="Fresh_alert",
        ),
        "Read_route": http("GET", route_uri(), managed=True, previous="Unhealthy_backend"),
        "Route_properties_safe": guard(
            "@and(equals(body('Read_route')?['properties']?['secret'], false), "
            f"not(empty({etag('Read_route')})), not(equals({etag('Read_route')}, '*')))",
            {}, previous="Read_route", code="UnsafeRoute",
            message="Route must be non-secret with a concrete ETag.",
        ),
        "Parse_route": parse(
            "@json(body('Read_route')['properties']['value'])", route_schema(),
            previous="Route_properties_safe",
        ),
        "Backup": compose(
            "@if(equals(body('Parse_route')['primary'], 'eastus2'), 'sweden', 'eastus2')",
            previous="Parse_route",
        ),
        "Route_eligible": guard(
            "@and(equals(outputs('Unhealthy_backend'), body('Parse_route')['primary']), "
            "contains(body('Parse_route')['enabled'], body('Parse_route')['primary']), "
            "contains(body('Parse_route')['enabled'], outputs('Backup')))",
            {}, previous="Backup", status="Cancelled", code="NotCurrentOrBackupDisabled",
            message="Alert is not for the current primary or backup is not enabled.",
        ),
        "Cooldown_elapsed": guard(
            cooldown, {}, previous="Route_eligible", status="Cancelled",
            code="CooldownActive", message="Cooldown active; no route change.",
        ),
        "Probe_one": probe(previous="Cooldown_elapsed"),
        "Probe_one_status": guard(
            "@equals(outputs('Probe_one')['statusCode'], 200)", {},
            previous="Probe_one", code="ProbeOneFailed",
            message="First real GPT probe did not return HTTP 200; route unchanged.",
        ),
        "Validate_probe_one": parse("@body('Probe_one')", probe_schema(), previous="Probe_one_status"),
        "Probe_two": probe(previous="Validate_probe_one"),
        "Probe_two_status": guard(
            "@equals(outputs('Probe_two')['statusCode'], 200)", {},
            previous="Probe_two", code="ProbeTwoFailed",
            message="Second real GPT probe did not return HTTP 200; route unchanged.",
        ),
        "Validate_probe_two": parse("@body('Probe_two')", probe_schema(), previous="Probe_two_status"),
        "Switch_enabled": guard(
            "@equals(parameters('switchEnabled'), true)", {},
            previous="Validate_probe_two", status="Cancelled", code="DryRunNoWrite",
            message="Both real GPT probes passed; switchEnabled is false, no route change.",
        ),
        "Still_fresh": guard(
            fresh_expression(), {}, previous="Switch_enabled", status="Cancelled",
            code="ExpiredBeforeWrite", message="Alert expired during probes; no route change.",
        ),
        "New_route": compose(updated_route_expression(), previous="Still_fresh"),
        "Write_route": http(
            "PUT", route_uri(), managed=True, previous="New_route",
            headers={"Content-Type": "application/json", "If-Match": "@" + etag("Read_route")},
            body={
                "properties": "@setProperty(body('Read_route')['properties'], "
                              "'value', string(outputs('New_route')))",
            },
        ),
        "Require_completed_write": guard(
            "@contains(createArray(200, 201), outputs('Write_route')['statusCode'])", {},
            previous="Write_route", code="WriteNotCompleted",
            message="ARM write is not synchronously complete; inspect control-plane state.",
        ),
        "Read_committed_route": http(
            "GET", route_uri(), managed=True, previous="Require_completed_write",
        ),
        "Verify_control_plane": guard(
            "@equals(body('Read_committed_route')['properties']['value'], string(outputs('New_route')))",
            {}, previous="Read_committed_route", code="ControlPlaneMismatch",
            message="ARM read-back differs; gateway propagation has NOT been verified.",
        ),
    }


def workflow_definition():
    return {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/"
                   "schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "executorKey": {"type": "SecureString"},
            "executorBaseUri": {"type": "String"},
            "workspaceId": {"type": "String"},
            "switchEnabled": {"type": "Bool", "defaultValue": False},
        },
        "triggers": {
            "monitor_alert": {
                "type": "Request", "kind": "Http",
                "inputs": {"method": "POST", "schema": {"type": "object"}},
                "runtimeConfiguration": {
                    "concurrency": {"runs": 1, "maximumWaitingRuns": 10},
                    **copy.deepcopy(SECURE),
                },
            },
        },
        "actions": {
            "Access_check_or_alert": {
                "type": "If",
                "expression": "@equals(triggerBody()?['schemaId'], 'monitoring.accessCheck.v1')",
                "actions": access_check_actions(),
                "else": {"actions": failover_actions()},
                "runAfter": {},
            },
        },
        "outputs": {},
    }


def alert_query(kind):
    if kind not in ("latency", "errors"):
        raise ValueError("Unknown alert kind")
    violation = "AverageBackendMs >= 3200" if kind == "latency" else "ErrorRatePct >= 20.0"
    return "\n".join([
        "ApiManagementGatewayLogs",
        "| where TimeGenerated >= ago(5m)",
        f"| where _ResourceId =~ '{APIM}' and ApiId == 'llm'",
        r'| extend Backend = extract(@"/execute/(eastus2|sweden)(?:[/?#]|$)", 1, tostring(BackendUrl))',
        "| where Backend in ('eastus2', 'sweden')",
        "| summarize Samples = count(), AverageBackendMs = avg(todouble(BackendTime)),",
        "    ErrorCount = countif(toint(ResponseCode) == 429 or toint(ResponseCode) between (500 .. 599))",
        "    by Backend",
        "| extend ErrorRatePct = 100.0 * ErrorCount / Samples",
        f"| extend ViolationCount = iff(Samples >= 5 and {violation}, 1, 0)",
        "| project Backend, ViolationCount",
    ])


def scheduled_rule(kind, enabled=False):
    return {
        "location": LOCATION, "kind": "LogAlert",
        "properties": {
            "displayName": "llm-backend-" + kind,
            "description": "Five-minute per-backend APIM " + kind + "; managed-identity failover only.",
            "enabled": enabled, "severity": 2,
            "evaluationFrequency": "PT1M", "windowSize": "PT5M",
            "scopes": [WORKSPACE], "autoMitigate": True, "skipQueryValidation": False,
            "criteria": {
                "allOf": [{
                    "query": alert_query(kind), "timeAggregation": "Maximum",
                    "metricMeasureColumn": "ViolationCount", "operator": "GreaterThan",
                    "threshold": 0,
                    "dimensions": [{
                        "name": "Backend", "operator": "Include", "values": list(BACKENDS),
                    }],
                    "failingPeriods": {
                        "numberOfEvaluationPeriods": 1, "minFailingPeriodsToAlert": 1,
                    },
                }],
            },
            "actions": {"actionGroups": [ACTION_GROUP]},
        },
    }


def deployment_template():
    workflow_resource = {
        "type": "Microsoft.Logic/workflows", "apiVersion": LOGIC_API,
        "name": WORKFLOW_NAME, "location": LOCATION,
        "identity": {"type": "SystemAssigned"},
        "properties": {
            "state": "Enabled", "definition": workflow_definition(),
            "parameters": {
                "executorKey": {"value": "[parameters('executorKey')]"},
                "executorBaseUri": {"value": "[parameters('executorBaseUri')]"},
                "workspaceId": {"value": WORKSPACE},
                "switchEnabled": {"value": False},
            },
        },
    }
    group = {
        "type": "Microsoft.Insights/actionGroups", "apiVersion": ACTION_GROUP_API,
        "name": ACTION_GROUP_NAME, "location": "Global", "dependsOn": [WORKFLOW],
        "properties": {
            "groupShortName": "llmfailover", "enabled": True,
            "logicAppReceivers": [{
                "name": "verified-backup-switch", "resourceId": WORKFLOW,
                "callbackUrl": "[listCallbackUrl(concat(resourceId('Microsoft.Logic/workflows', '"
                               + WORKFLOW_NAME + "'), '/triggers/monitor_alert'), '2016-06-01').value]",
                "useCommonAlertSchema": True,
            }],
        },
    }
    rules = [
        {
            "type": "Microsoft.Insights/scheduledQueryRules", "apiVersion": ALERT_API,
            "name": "llm-backend-" + kind, "dependsOn": [ACTION_GROUP],
            **scheduled_rule(kind),
        }
        for kind in ("latency", "errors")
    ]
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "executorKey": {"type": "secureString"},
            "executorBaseUri": {"type": "string"},
        },
        "resources": [workflow_resource, group, *rules], "outputs": {},
    }


def deploy():
    app = arm("GET", APP, version="2024-03-01")
    fqdn = app["properties"]["configuration"]["ingress"]["fqdn"]
    if not fqdn or "/" in fqdn or not fqdn.endswith(".azurecontainerapps.io"):
        raise ValueError("Unexpected executor ingress host")
    key = container_secrets()["executor-key"]
    if not key:
        raise ValueError("Executor key is missing")
    body = {
        "properties": {
            "mode": "Incremental", "template": deployment_template(),
            "parameters": {
                "executorKey": {"value": key},
                "executorBaseUri": {"value": "https://" + fqdn},
            },
        },
    }
    deployment_id = ROOT + "/providers/Microsoft.Resources/deployments/llm-monitoring"
    validation = arm("POST", deployment_id + "/validate", body, DEPLOY_API)
    if validation.get("error") or validation.get("properties", {}).get("error"):
        raise RuntimeError("ARM template validation failed; inspect sanitized deployment diagnostics.")
    arm("PUT", deployment_id, body, DEPLOY_API)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        state = arm("GET", deployment_id, version=DEPLOY_API)["properties"]["provisioningState"]
        if state == "Succeeded":
            info = arm("GET", WORKFLOW, version=LOGIC_API)
            require_disabled_alerts()
            print("Deployed; both alerts DISABLED; switchEnabled=false.")
            print("Managed identity principal ID: " + info["identity"]["principalId"])
            print("Administrator grant required at scope: " + ROUTE)
            return
        if state in ("Failed", "Canceled"):
            raise RuntimeError("Monitoring deployment did not succeed; alerts must remain disabled.")
        time.sleep(2)
    raise RuntimeError("Deployment still running; inspect status before retrying, do not enable alerts.")


def alert_id(name):
    return ROOT + "/providers/Microsoft.Insights/scheduledQueryRules/" + name


def require_disabled_alerts():
    for name in ALERT_NAMES:
        rule = arm("GET", alert_id(name), version=ALERT_API)
        if rule["properties"]["enabled"] is not False:
            raise RuntimeError("Disable both monitoring alerts before checking managed identity access.")


def require_enabled_alerts():
    for name in ALERT_NAMES:
        rule = arm("GET", alert_id(name), version=ALERT_API)
        if rule["properties"]["enabled"] is not True:
            raise RuntimeError("Alert activation read-back did not match; restore disabled defaults.")


def invoke_and_wait(payload, *, return_run=False):
    callback = arm(
        "POST", WORKFLOW + "/triggers/monitor_alert/listCallbackUrl", {}, "2016-06-01",
    )["value"]
    tracking = str(uuid.uuid4())
    request = urllib.request.Request(
        callback, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-ms-client-tracking-id": tracking},
    )
    try:
        with urllib.request.urlopen(request, timeout=130) as response:
            if response.status not in (200, 202):
                raise RuntimeError("Workflow trigger was not accepted.")
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Workflow trigger failed or timed out; inspect status before retrying.") from None
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        runs = arm("GET", WORKFLOW + "/runs", version=LOGIC_API)["value"]
        for run in runs:
            props = run["properties"]
            if props.get("correlation", {}).get("clientTrackingId") != tracking:
                continue
            status = props["status"]
            if status in ("Succeeded", "Failed", "Cancelled", "TimedOut", "Aborted"):
                print("Workflow run status: " + status + "; run ID: " + run["name"])
                return (status, run["name"]) if return_run else status
        time.sleep(2)
    raise RuntimeError("Workflow still running or not found; alerts remain disabled.")


def inspect_run(run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise ValueError("Invalid workflow run ID")
    path = WORKFLOW + "/runs/" + run_id
    run = arm("GET", path, version=LOGIC_API)
    actions_path = path + "/actions"
    result = arm("GET", actions_path, version=LOGIC_API)
    statuses = {}
    pages = 0
    while True:
        statuses.update({item["name"]: item["properties"]["status"] for item in result["value"]})
        link = result.get("nextLink")
        if not link:
            break
        pages += 1
        if pages >= 20:
            raise RuntimeError("Action status pagination exceeded bound; no-write proof is incomplete.")
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme != "https" or parsed.netloc not in ("management.azure.com", "management.azure.com:443") \
                or parsed.path.lower() != actions_path.lower():
            raise RuntimeError("Unexpected action-status pagination resource; inspection stopped.")
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        versions = [v for k, v in query if k.lower() == "api-version"]
        if versions != [LOGIC_API]:
            raise RuntimeError("Unexpected action-status pagination API version.")
        remaining = urllib.parse.urlencode([(k, v) for k, v in query if k.lower() != "api-version"])
        # azure.arm builds '?api-version=' itself; preserve the service's
        # continuation query in memory without exposing its skip token.
        result = arm("GET", actions_path, version=LOGIC_API + ("&" + remaining if remaining else ""))
    print("Run " + run_id + ": " + run["properties"]["status"])
    for name, state in sorted(statuses.items()):
        print("  " + name + ": " + state)
    return run["properties"]["status"], statuses


def ignored_smoke():
    """Exercise only the Resolved guard; never invoke MI ARM or model actions."""
    require_disabled_alerts()
    workflow = arm("GET", WORKFLOW, version=LOGIC_API)
    if workflow["properties"]["parameters"]["switchEnabled"]["value"] is not False:
        raise RuntimeError("Ignored smoke requires switchEnabled=false; disable the controller first.")
    payload = {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": {
                "alertRule": ALERT_NAMES[0], "monitorCondition": "Resolved",
                "alertTargetIDs": [WORKSPACE],
                "firedDateTime": datetime.now(timezone.utc).isoformat(),
            },
            "alertContext": {
                "condition": {"allOf": [
                    {"dimensions": [{"name": "Backend", "value": "eastus2"}]},
                ]},
            },
        },
    }
    outcome, run_id = invoke_and_wait(payload, return_run=True)
    actual, states = inspect_run(run_id)
    if outcome != "Cancelled" or actual != "Cancelled":
        raise RuntimeError("Ignored smoke did not take the expected cancelled no-op branch.")
    if states.get("ResolvedNoOp") not in ("Succeeded", "Cancelled"):
        raise RuntimeError("ResolvedNoOp action was not observed; no-write proof is incomplete.")
    forbidden = (
        "Read_route", "Read_access_route", "Read_committed_route",
        "Write_route", "Verify_access_write", "Probe_one", "Probe_two",
    )
    if any(states.get(name, "Skipped") != "Skipped" for name in forbidden):
        raise RuntimeError("Unexpected ARM or probe action execution; inspect controller before proceeding.")
    print("Ignored Resolved smoke verified: no workflow ARM reads/writes or model probes executed.")
    print("This proves the no-op guard only, not managed-identity access or failover.")
    return run_id


def verify_access():
    require_disabled_alerts()
    switch_mode(False)
    if invoke_and_wait({"schemaId": "monitoring.accessCheck.v1"}) != "Succeeded":
        raise RuntimeError(
            "Managed identity access NOT verified. No credentials fallback; ask an administrator "
            "to run grant-route-role.sh. Alerts remain disabled."
        )
    print("MI GET and ETag-guarded unchanged-value PUT verified. This is NOT a GPT health check.")


def switch_mode(enabled):
    # Logic Apps rejects PATCH of properties. PUT preserves the deployed
    # definition and rehydrates the secure parameter only in memory.
    workflow = arm("GET", WORKFLOW, version=LOGIC_API)
    parameters = copy.deepcopy(workflow["properties"]["parameters"])
    parameters["executorKey"] = {"value": container_secrets()["executor-key"]}
    parameters["switchEnabled"] = {"value": enabled}
    arm("PUT", WORKFLOW, {
        "location": workflow["location"],
        "identity": {"type": "SystemAssigned"},
        "tags": workflow.get("tags", {}),
        "properties": {
            "definition": workflow["properties"]["definition"],
            "state": workflow["properties"]["state"],
            "parameters": parameters,
        },
    }, LOGIC_API)
    result = arm("GET", WORKFLOW, version=LOGIC_API)
    if result["properties"]["parameters"]["switchEnabled"]["value"] is not enabled:
        raise RuntimeError("Workflow activation parameter read-back did not match.")


def disable():
    for kind in ("latency", "errors"):
        arm("PUT", alert_id("llm-backend-" + kind), scheduled_rule(kind, False), ALERT_API)
    switch_mode(False)
    require_disabled_alerts()
    print("Alerts DISABLED; workflow is probe-only (no alert-driven route writes).")


def status():
    workflow = arm("GET", WORKFLOW, version=LOGIC_API)
    print("Workflow state: " + workflow["properties"]["state"])
    print("Managed identity principal ID: " + workflow["identity"]["principalId"])
    print("switchEnabled: " + str(
        workflow["properties"]["parameters"]["switchEnabled"]["value"],
    ))
    for name in ALERT_NAMES:
        rule = arm("GET", alert_id(name), version=ALERT_API)
        print(name + ": " + ("ENABLED" if rule["properties"]["enabled"] else "DISABLED"))
    runs = arm("GET", WORKFLOW + "/runs", version=LOGIC_API)
    for run in runs["value"][:5]:
        print("Run " + run["name"] + ": " + run["properties"]["status"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "template", "deploy", "status", "verify-access", "enable-alerts", "disable", "probe-only",
        "smoke-ignored", "inspect-run",
    ))
    parser.add_argument("--run-id", help="Workflow run ID for safe action-status inspection")
    parser.add_argument("--event-file", type=Path, help="Common-schema Fired event for probe-only")
    parser.add_argument("--confirm-log-schema", action="store_true",
                        help="Actual APIM log fields, BackendUrl, units and alert payload checked")
    args = parser.parse_args()
    try:
        if args.command == "template":
            print(json.dumps(deployment_template(), indent=2))
        elif args.command == "deploy":
            deploy()
        elif args.command == "status":
            status()
        elif args.command == "smoke-ignored":
            ignored_smoke()
        elif args.command == "inspect-run":
            if not args.run_id:
                raise ValueError("--run-id is required")
            inspect_run(args.run_id)
        elif args.command == "verify-access":
            verify_access()
        elif args.command == "disable":
            disable()
        elif args.command == "enable-alerts":
            if not args.confirm_log_schema:
                raise ValueError("--confirm-log-schema required after checking real telemetry and payloads")
            verify_access()
            switch_mode(True)
            try:
                for kind in ("latency", "errors"):
                    arm("PUT", alert_id("llm-backend-" + kind), scheduled_rule(kind, True), ALERT_API)
                require_enabled_alerts()
            except Exception:
                disable()
                raise
            print("Alerts enabled after MI read/write verification; gateway propagation not verified.")
        elif args.command == "probe-only":
            if not args.event_file:
                raise ValueError("--event-file is required")
            require_disabled_alerts()
            switch_mode(False)
            payload = json.loads(args.event_file.read_text(encoding="utf-8"))
            if payload.get("schemaId") != "azureMonitorCommonAlertSchema":
                raise ValueError("probe-only accepts only a common-schema alert, never accessCheck")
            outcome = invoke_and_wait(payload)
            if outcome == "Failed":
                raise RuntimeError("Probe-only run failed; inspect secured action status, not raw inputs.")
            print("Probe-only cannot write the route. Cancelled may mean dry-run OR rejected alert; "
                  "inspect action statuses to distinguish.")
    except Exception as exc:
        # Do not print arbitrary SDK/HTTP exception messages; they may contain URLs or secrets.
        print("Monitoring command failed (" + type(exc).__name__ + ").", file=sys.stderr)
        if type(exc) is RuntimeError:
            # All RuntimeErrors raised here or by azure.arm contain fixed,
            # sanitized text, never a raw HTTP/SDK response or a callback URL.
            print(str(exc), file=sys.stderr)
        print("Keep alerts disabled; check access grant, secure run/deployment status and command prerequisites.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
