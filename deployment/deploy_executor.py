"""Deploy the lab executor without weakening existing Foundry authentication."""

import json
import secrets
from pathlib import Path

from azure import ACR, APP, MI, RG, ROOT, arm, cli

config = json.loads(Path(__file__).with_name("config.json").read_text())
token = cli(
    "acr", "token", "credential", "generate", "-r", "acrsvhwb1070915",
    "-n", "svhwb107pull", "--password1", "--expiration-in-days", "30",
)
password = token["passwords"][0]["value"]
executor_key = secrets.token_urlsafe(48)
body = {
    "location": "eastus2",
    "tags": {"issue": "SVHWB-107", "purpose": "latency-failover-validation"},
    "identity": {"type": "UserAssigned", "userAssignedIdentities": {MI: {}}},
    "properties": {
        "managedEnvironmentId": ROOT + "/providers/Microsoft.App/managedEnvironments/cae-svhwb107",
        "configuration": {
            "activeRevisionsMode": "Single",
            "secrets": [
                {"name": "executor-key", "value": executor_key},
                {"name": "registry-pull", "value": password},
            ],
            "ingress": {
                "external": True, "targetPort": 8000, "transport": "http",
                "allowInsecure": False,
            },
            "registries": [{
                "server": "acrsvhwb1070915.azurecr.io",
                "username": "svhwb107pull", "passwordSecretRef": "registry-pull",
            }],
        },
        "template": {
            "containers": [{
                "name": "executor",
                "image": "acrsvhwb1070915.azurecr.io/executor:v1",
                "env": [
                    {"name": "PORT", "value": "8000"},
                    {"name": "AZURE_CLIENT_ID", "value": "3dfb189c-3492-4eb9-80f8-4bc97213bf76"},
                    {"name": "EXECUTOR_KEY", "secretRef": "executor-key"},
                    {"name": "BACKENDS_JSON", "value": json.dumps(config["backends"])},
                    {"name": "ENABLE_FAULTS", "value": "true"},
                ],
                "resources": {"cpu": 0.5, "memory": "1Gi"},
            }],
            "scale": {"minReplicas": 1, "maxReplicas": 2},
        },
    },
}
result = arm("PUT", APP, body, "2024-03-01")
print(json.dumps({
    "id": result["id"],
    "state": result["properties"]["provisioningState"],
    "fqdn": result["properties"]["configuration"]["ingress"].get("fqdn"),
}))

# Azure Monitor diagnostics, with no key written to a local file.
workspace = ROOT + "/providers/Microsoft.OperationalInsights/workspaces/law-svhwb107-0915"
arm("PUT", ROOT + "/providers/Microsoft.App/managedEnvironments/cae-svhwb107"
    + "/providers/Microsoft.Insights/diagnosticSettings/lab-logs", {
        "properties": {
            "workspaceId": workspace,
            "logs": [
                {"category": "ContainerAppConsoleLogs", "enabled": True},
                {"category": "ContainerAppSystemLogs", "enabled": True},
            ],
        },
    }, "2021-05-01-preview")
