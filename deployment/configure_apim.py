"""Configure only the isolated SVHWB-107 gateway; no production policy edits."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from azure import APIM, APP, ROOT, arm, container_secrets


def put(path, properties):
    return arm("PUT", APIM + path, {"properties": properties})


app = arm("GET", APP, version="2024-03-01")
host = app["properties"]["configuration"]["ingress"]["fqdn"]
put("/namedValues/executor-key", {
    "displayName": "executor-key", "secret": True,
    "value": container_secrets()["executor-key"],
})
put("/namedValues/executor-base", {
    "displayName": "executor-base", "secret": False, "value": "https://" + host,
})
put("/namedValues/chat-route", {
    "displayName": "chat-route", "secret": False,
    "value": '{"primary":"eastus2","enabled":["eastus2","sweden"],"version":1}',
})
put("/apis/llm", {
    "displayName": "SVHWB107 bounded LLM",
    "path": "llm", "protocols": ["https"], "subscriptionRequired": True,
})
for purpose in ("intent", "rewrite", "generate", "embedding"):
    put(f"/apis/llm/operations/{purpose}", {
        "displayName": purpose, "method": "POST",
        "urlTemplate": "/" + purpose, "responses": [],
    })
put("/apis/validation", {
    "displayName": "SVHWB107 isolated fault validation",
    "path": "validation", "protocols": ["https"], "subscriptionRequired": True,
})
put("/apis/validation/operations/fault", {
    "displayName": "fault", "method": "POST", "urlTemplate": "/{scenario}",
    "templateParameters": [{"name": "scenario", "required": True, "type": "string"}],
    "responses": [],
})

for api, validation in (("llm", False), ("validation", True)):
    policy = ET.Element("policies")
    inbound = ET.SubElement(policy, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "rate-limit-by-key", {
        "calls": "30", "renewal-period": "60",
        "counter-key": '@("svhwb107-" + context.Subscription.Id)',
    })
    ET.SubElement(inbound, "set-variable", {
        "name": "purpose", "value": "generate" if validation else "@(context.Operation.Id)",
    })
    if not validation:
        choose = ET.SubElement(inbound, "choose")
        bad_model = ET.SubElement(choose, "when", {
            "condition": '@{ var body = context.Request.Body.As<JObject>(preserveContent: true); '
            'var model = (string)body["model"]; var expected = context.Operation.Id == "embedding" '
            '? "text-embedding-3-small" : "gpt-5.1"; return model != null && model != expected; }',
        })
        response = ET.SubElement(bad_model, "return-response")
        ET.SubElement(response, "set-status", {"code": "400", "reason": "Unsupported model"})
        ET.SubElement(inbound, "set-body").text = (
            '@{ var body = context.Request.Body.As<JObject>(); body.Remove("model"); return body.ToString(); }'
        )
    ET.SubElement(inbound, "set-variable", {
        "name": "budget", "value": '@((string)context.Variables["purpose"] == "rewrite" ? 1200 : '
        '(string)context.Variables["purpose"] == "embedding" ? 500 : '
        '(string)context.Variables["purpose"] == "intent" ? 5000 : 4000)',
    })
    ET.SubElement(inbound, "set-variable", {
        "name": "budget",
        "value": '@{ int remaining; var raw = context.Request.Headers.GetValueOrDefault("X-Remaining-Budget-Ms", ""); '
        'if (int.TryParse(raw, out remaining)) { return Math.Max(1, Math.Min(remaining, (int)context.Variables["budget"])); } '
        'return (int)context.Variables["budget"]; }',
    })
    ET.SubElement(inbound, "set-variable", {
        "name": "route", "value": '{{chat-route}}',
    })
    ET.SubElement(inbound, "set-variable", {"name": "attempt", "value": "@(0)"})
    choose = ET.SubElement(inbound, "choose")
    when = ET.SubElement(choose, "when", {
        "condition": '@((string)context.Variables["purpose"] != "embedding" && '
        '((JArray)JObject.Parse((string)context.Variables["route"])["enabled"]).Count == 0)',
    })
    ret = ET.SubElement(when, "return-response")
    ET.SubElement(ret, "set-status", {"code": "503", "reason": "No healthy backends"})

    backend = ET.SubElement(policy, "backend")
    condition = (
        '@(context.Response != null && '
        '(context.Response.StatusCode == 429 || context.Response.StatusCode >= 500) && '
        '(string)context.Variables["purpose"] != "rewrite" && '
        '(string)context.Variables["purpose"] != "embedding" && '
        '((JArray)JObject.Parse((string)context.Variables["route"])["enabled"]).Count > 1 && '
        '(int)context.Variables["budget"] - (DateTime.UtcNow - context.Timestamp).TotalMilliseconds > 2700)'
    )
    retry = ET.SubElement(backend, "retry", {
        "condition": condition, "count": "1", "interval": "1", "first-fast-retry": "true",
    })
    ET.SubElement(retry, "set-variable", {
        "name": "attempt", "value": '@((int)context.Variables["attempt"] + 1)',
    })
    ET.SubElement(retry, "set-variable", {
        "name": "selected", "value": '@{'
        'if ((string)context.Variables["purpose"] == "embedding") { return "embedding"; } '
        'var route = JObject.Parse((string)context.Variables["route"]); '
        'var primary = (string)route["primary"]; '
        'return (int)context.Variables["attempt"] == 1 ? primary : (primary == "eastus2" ? "sweden" : "eastus2"); }',
    })
    if validation:
        suffix = '@("/fault/" + ((int)context.Variables["attempt"] == 1 ? context.Request.MatchedParameters["scenario"] : "success"))'
    else:
        suffix = '@("/execute/" + (string)context.Variables["selected"])'
    ET.SubElement(retry, "set-backend-service", {"base-url": "{{executor-base}}"})
    ET.SubElement(retry, "rewrite-uri", {"template": suffix, "copy-unmatched-params": "false"})
    for name, value in (
        ("X-Executor-Key", "{{executor-key}}"),
        ("X-Budget-Ms", '@(Math.Max(1, (int)context.Variables["budget"] - (int)(DateTime.UtcNow - context.Timestamp).TotalMilliseconds - 250).ToString())'),
        ("X-Idle-Ms", "700" if validation else "1200"),
        ("X-Request-Id", "@(context.RequestId.ToString())"),
    ):
        header = ET.SubElement(retry, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(header, "value").text = value
    for name in ("Ocp-Apim-Subscription-Key", "Authorization", "api-key"):
        ET.SubElement(retry, "set-header", {"name": name, "exists-action": "delete"})
    ET.SubElement(retry, "forward-request", {
        "timeout": '@(Math.Max(1, (int)Math.Ceiling(((int)context.Variables["budget"] - (DateTime.UtcNow - context.Timestamp).TotalMilliseconds) / 1000.0)))',
        "buffer-request-body": "true", "buffer-response": "true",
        "fail-on-error-status-code": "false",
    })
    outbound = ET.SubElement(policy, "outbound")
    ET.SubElement(outbound, "base")
    for name, value in (
        ("X-Lab-Attempts", '@(((int)context.Variables["attempt"]).ToString())'),
        ("X-Lab-Backend", '@((string)context.Variables["selected"])'),
        ("X-Lab-Request-Id", "@(context.RequestId.ToString())"),
    ):
        h = ET.SubElement(outbound, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(h, "value").text = value
    on_error = ET.SubElement(policy, "on-error")
    ET.SubElement(on_error, "base")
    for name, expr in (
        ("X-Lab-Error-Reason", "@(context.LastError.Reason)"),
        ("X-Lab-Error-Source", "@(context.LastError.Source)"),
        ("X-Lab-Error-Path", "@(context.LastError.Path)"),
        ("X-Lab-Error-Detail", '@(context.LastError.Source == "forward-request" ? context.LastError.Message : "")'),
    ):
        h = ET.SubElement(on_error, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(h, "value").text = expr
    xml = ET.tostring(policy, encoding="unicode")
    Path(__file__).with_name(api + "-policy.xml").write_text(xml, encoding="utf-8")
    put(f"/apis/{api}/policies/policy", {"format": "rawxml", "value": xml})

put("/subscriptions/lab-validation", {
    "displayName": "SVHWB107 validation client", "scope": "/apis", "state": "active",
})
workspace = ROOT + "/providers/Microsoft.OperationalInsights/workspaces/law-svhwb107-0915"
arm("PUT", APIM + "/providers/Microsoft.Insights/diagnosticSettings/lab-logs", {
    "properties": {
        "workspaceId": workspace,
        "logAnalyticsDestinationType": "Dedicated",
        "logs": [{"category": "GatewayLogs", "enabled": True}],
        "metrics": [{"category": "AllMetrics", "enabled": True}],
    },
}, "2021-05-01-preview")
put("/loggers/azuremonitor", {
    "loggerType": "azureMonitor", "description": "SVHWB107 Azure Monitor diagnostics",
})
put("/diagnostics/azuremonitor", {
    "loggerId": APIM + "/loggers/azuremonitor",
    "alwaysLog": "allErrors", "sampling": {"samplingType": "fixed", "percentage": 100},
    "logClientIp": False, "verbosity": "information",
    "frontend": {
        "request": {"headers": [], "body": {"bytes": 0}},
        "response": {"headers": ["X-Lab-Backend", "X-Lab-Attempts"], "body": {"bytes": 0}},
    },
    "backend": {
        "request": {"headers": [], "body": {"bytes": 0}},
        "response": {"headers": ["x-executor-error", "x-executor-attempt-id"], "body": {"bytes": 0}},
    },
})
print("APIM APIs, bounded policies, secret reference, subscription and diagnostics configured.")
