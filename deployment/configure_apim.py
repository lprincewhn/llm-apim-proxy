"""Configure the single-attempt business API and its Azure Monitor diagnostics."""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from azure import APIM, ROOT, arm
from backends import MODEL_IDENTITY_CLIENT_ID, MODEL_IDENTITY_ID, load_backends


def native_operations():
    return {
        "native-" + name: {
            "displayName": "Azure OpenAI " + name,
            "method": "POST",
            "urlTemplate": "/deployments/" + backend["deployment"] + "/"
            + ("embeddings" if backend["kind"] == "embedding" else "chat/completions"),
            "responses": [],
        }
        for name, backend in load_backends().items()
    }


def build_policy():
    backends = load_backends()
    policy = ET.Element("policies")
    inbound = ET.SubElement(policy, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "rate-limit-by-key", {
        "calls": "30", "renewal-period": "60",
        "counter-key": '@("svhwb107-" + context.Subscription.Id)',
    })
    ET.SubElement(inbound, "set-variable", {"name": "route", "value": "{{chat-route}}"})
    choose = ET.SubElement(inbound, "choose")
    when = ET.SubElement(choose, "when", {
        "condition": '@(context.Operation.Id != "native-embedding" && '
        '!((JArray)JObject.Parse((string)context.Variables["route"])["enabled"]).Any(item => (string)item == '
        '(string)JObject.Parse((string)context.Variables["route"])["primary"]))',
    })
    ret = ET.SubElement(when, "return-response")
    ET.SubElement(ret, "set-status", {"code": "503", "reason": "No healthy backends"})
    ET.SubElement(inbound, "set-variable", {
        "name": "selected", "value": '@{'
        'if (context.Operation.Id == "native-embedding") { return "embedding"; } '
        'return (string)JObject.Parse((string)context.Variables["route"])["primary"]; }',
    })
    targets = ET.SubElement(inbound, "choose")
    for name, backend_config in backends.items():
        target = ET.SubElement(targets, "when", {
            "condition": f'@((string)context.Variables["selected"] == "{name}")',
        })
        ET.SubElement(target, "set-backend-service", {"backend-id": "llm-" + name})
        operation = "embeddings" if backend_config["kind"] == "embedding" else "chat/completions"
        ET.SubElement(target, "rewrite-uri", {
            "template": "/openai/deployments/" + backend_config["deployment"] + "/" + operation,
            "copy-unmatched-params": "true",
        })
    invalid = ET.SubElement(targets, "otherwise")
    invalid_response = ET.SubElement(invalid, "return-response")
    ET.SubElement(invalid_response, "set-status", {"code": "503", "reason": "Unknown route backend"})
    ET.SubElement(inbound, "set-query-parameter", {
        "name": "subscription-key", "exists-action": "delete",
    })
    for name in (
        "Ocp-Apim-Subscription-Key", "Authorization", "api-key", "X-Executor-Key",
        "X-Budget-Ms", "X-Idle-Ms", "X-Remaining-Budget-Ms",
    ):
        ET.SubElement(inbound, "set-header", {"name": name, "exists-action": "delete"})
    ET.SubElement(inbound, "authentication-managed-identity", {
        "resource": "https://cognitiveservices.azure.com",
        "client-id": MODEL_IDENTITY_CLIENT_ID,
        "ignore-error": "false",
    })
    backend = ET.SubElement(policy, "backend")
    ET.SubElement(backend, "forward-request", {
        "timeout": "120",
        "buffer-request-body": "false", "buffer-response": "false",
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
    service = arm("GET", APIM)
    identity = service.get("identity") or {}
    identities = {resource_id: {} for resource_id in identity.get("userAssignedIdentities", {})}
    identities[MODEL_IDENTITY_ID] = {}
    identity_type = "SystemAssigned, UserAssigned" if "SystemAssigned" in identity.get("type", "") else "UserAssigned"
    if MODEL_IDENTITY_ID.lower() not in {
        resource_id.lower() for resource_id in identity.get("userAssignedIdentities", {})
    }:
        arm("PATCH", APIM, {"identity": {"type": identity_type, "userAssignedIdentities": identities}})
    for name, backend in load_backends().items():
        put("/backends/llm-" + name, {
            "protocol": "http", "url": backend["endpoint"].rstrip("/"),
            "description": "Direct Foundry " + name,
        })
    put("/apis/llm", {
        "displayName": "Azure OpenAI native proxy",
        "path": "openai", "protocols": ["https"], "subscriptionRequired": True,
        "subscriptionKeyParameterNames": {"header": "api-key", "query": "subscription-key"},
    })
    for operation_id, operation in native_operations().items():
        put(f"/apis/llm/operations/{operation_id}", operation)
    xml = build_policy()
    put("/apis/llm/policies/policy", {"format": "rawxml", "value": xml})
    Path(__file__).with_name("llm-policy.xml").write_text(xml, encoding="utf-8")
    operations = arm("GET", APIM + "/apis/llm/operations")["value"]
    for operation in operations:
        if operation["name"] in ("intent", "rewrite", "generate", "embedding"):
            arm("DELETE", APIM + "/apis/llm/operations/" + operation["name"],
                headers={"If-Match": "*"})
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
            "response": {"headers": ["x-request-id", "apim-request-id"], "body": {"bytes": 0}},
        },
    })
    print("Direct Foundry business API, model identity and Azure Monitor diagnostics configured.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize-route", action="store_true",
                        help="Explicitly initialize/reset route to East US 2; not for live recovery")
    configure(initialize_route=parser.parse_args().initialize_route)
