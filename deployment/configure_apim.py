"""Configure the single-attempt business API and its Azure Monitor diagnostics."""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from azure import APIM, APP, ROOT, arm, container_secrets


def build_policy():
    policy = ET.Element("policies")
    inbound = ET.SubElement(policy, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "rate-limit-by-key", {
        "calls": "30", "renewal-period": "60",
        "counter-key": '@("svhwb107-" + context.Subscription.Id)',
    })
    ET.SubElement(inbound, "set-variable", {
        "name": "purpose", "value": "@(context.Operation.Id)",
    })
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
    ET.SubElement(inbound, "set-variable", {"name": "route", "value": "{{chat-route}}"})
    choose = ET.SubElement(inbound, "choose")
    when = ET.SubElement(choose, "when", {
        "condition": '@((string)context.Variables["purpose"] != "embedding" && '
        '((JArray)JObject.Parse((string)context.Variables["route"])["enabled"]).Count == 0)',
    })
    ret = ET.SubElement(when, "return-response")
    ET.SubElement(ret, "set-status", {"code": "503", "reason": "No healthy backends"})
    ET.SubElement(inbound, "set-variable", {
        "name": "selected", "value": '@{'
        'if ((string)context.Variables["purpose"] == "embedding") { return "embedding"; } '
        'return (string)JObject.Parse((string)context.Variables["route"])["primary"]; }',
    })
    ET.SubElement(inbound, "set-backend-service", {"base-url": "{{executor-base}}"})
    ET.SubElement(inbound, "rewrite-uri", {
        "template": '@("/execute/" + (string)context.Variables["selected"])',
        "copy-unmatched-params": "false",
    })
    for name, value in (
        ("X-Executor-Key", "{{executor-key}}"),
        ("X-Budget-Ms", '@(Math.Max(1, (int)context.Variables["budget"] - (int)(DateTime.UtcNow - context.Timestamp).TotalMilliseconds - 250).ToString())'),
        ("X-Idle-Ms", "1200"),
        ("X-Request-Id", "@(context.RequestId.ToString())"),
    ):
        header = ET.SubElement(inbound, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(header, "value").text = value
    for name in ("Ocp-Apim-Subscription-Key", "Authorization", "api-key"):
        ET.SubElement(inbound, "set-header", {"name": name, "exists-action": "delete"})
    backend = ET.SubElement(policy, "backend")
    ET.SubElement(backend, "forward-request", {
        "timeout": '@(Math.Max(1, (int)Math.Ceiling(((int)context.Variables["budget"] - (DateTime.UtcNow - context.Timestamp).TotalMilliseconds) / 1000.0)))',
        "buffer-request-body": "true", "buffer-response": "true",
        "fail-on-error-status-code": "false",
    })
    outbound = ET.SubElement(policy, "outbound")
    ET.SubElement(outbound, "base")
    for name, value in (
        ("X-Lab-Attempts", "1"),
        ("X-Lab-Backend", '@((string)context.Variables["selected"])'),
        ("X-Lab-Request-Id", "@(context.RequestId.ToString())"),
    ):
        header = ET.SubElement(outbound, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(header, "value").text = value
    on_error = ET.SubElement(policy, "on-error")
    ET.SubElement(on_error, "base")
    for name, expression in (
        ("X-Lab-Error-Reason", "@(context.LastError.Reason)"),
        ("X-Lab-Error-Source", "@(context.LastError.Source)"),
        ("X-Lab-Error-Path", "@(context.LastError.Path)"),
        ("X-Lab-Error-Detail", '@(context.LastError.Source == "forward-request" ? context.LastError.Message : "")'),
    ):
        header = ET.SubElement(on_error, "set-header", {"name": name, "exists-action": "override"})
        ET.SubElement(header, "value").text = expression
    ET.indent(policy)
    return ET.tostring(policy, encoding="unicode") + "\n"


def configure(*, initialize_route=False):
    def put(path, properties):
        return arm("PUT", APIM + path, {"properties": properties})

    if initialize_route:
        put("/namedValues/chat-route", {
            "displayName": "chat-route", "secret": False,
            "value": '{"primary":"eastus2","enabled":["eastus2","sweden"],"version":1}',
        })
    else:
        # Fail if the prerequisite is missing; never silently reset a live quarantine.
        arm("GET", APIM + "/namedValues/chat-route")
    app = arm("GET", APP, version="2024-03-01")
    host = app["properties"]["configuration"]["ingress"]["fqdn"]
    put("/namedValues/executor-key", {
        "displayName": "executor-key", "secret": True,
        "value": container_secrets()["executor-key"],
    })
    put("/namedValues/executor-base", {
        "displayName": "executor-base", "secret": False, "value": "https://" + host,
    })
    put("/apis/llm", {
        "displayName": "SVHWB107 single-attempt LLM",
        "path": "llm", "protocols": ["https"], "subscriptionRequired": True,
    })
    for purpose in ("intent", "rewrite", "generate", "embedding"):
        put(f"/apis/llm/operations/{purpose}", {
            "displayName": purpose, "method": "POST",
            "urlTemplate": "/" + purpose, "responses": [],
        })
    xml = build_policy()
    put("/apis/llm/policies/policy", {"format": "rawxml", "value": xml})
    Path(__file__).with_name("llm-policy.xml").write_text(xml, encoding="utf-8")
    put("/subscriptions/lab-validation", {
        "displayName": "SVHWB107 business client", "scope": "/apis", "state": "active",
    })
    workspace = ROOT + "/providers/Microsoft.OperationalInsights/workspaces/law-svhwb107-0915"
    arm("PUT", APIM + "/providers/Microsoft.Insights/diagnosticSettings/lab-logs", {
        "properties": {
            "workspaceId": workspace, "logAnalyticsDestinationType": "Dedicated",
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
    print("Single-attempt business API and Azure Monitor diagnostics configured.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize-route", action="store_true",
                        help="Explicitly initialize/reset route to East US 2; not for live recovery")
    configure(initialize_route=parser.parse_args().initialize_route)
