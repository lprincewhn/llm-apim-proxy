"""Opt-in SVHWB-124 metric-alert definition; never changes production on import."""

import copy

import deploy


ACCOUNT = (
    "/subscriptions/10564893-ecc3-4a6d-b505-53bcbe89dd8e"
    "/resourceGroups/jump-server_group/providers/Microsoft.CognitiveServices"
    "/accounts/svhw-openai-eastus2"
)
RULE_NAME = "svhwb124-foundry-404-r3"
RULE = deploy.ROOT + "/providers/Microsoft.Insights/metricAlerts/" + RULE_NAME
METRIC_API = "2018-03-01"
DIMENSIONS = {
    "ApiName": "OpenAI",
    "ModelDeploymentName": "gpt-5.1",
    "OperationName": "chatcompletions_create",
    "StatusCode": "404",
}


def metric_rule(enabled=False):
    """Five 404s, NOT the production rule's 20% combined error ratio."""
    return {
        "location": "global",
        "tags": {"issue": "SVHWB-124", "purpose": "temporary-ab-experiment"},
        "properties": {
            "description": "SVHWB-124 B: Foundry deployment chat 404 count >=5/5m; not error rate.",
            "severity": 2,
            "enabled": enabled,
            "scopes": [ACCOUNT],
            "evaluationFrequency": "PT1M",
            "windowSize": "PT5M",
            "autoMitigate": True,
            "criteria": {
                "odata.type": "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria",
                "allOf": [{
                    "name": "DeploymentChat404",
                    "metricNamespace": "Microsoft.CognitiveServices/accounts",
                    "metricName": "AzureOpenAIRequests",
                    "operator": "GreaterThanOrEqual",
                    "threshold": 5,
                    "timeAggregation": "Total",
                    "criterionType": "StaticThresholdCriterion",
                    "dimensions": [
                        {"name": name, "operator": "Include", "values": [value]}
                        for name, value in DIMENSIONS.items()
                    ],
                }],
            },
            "actions": [{"actionGroupId": deploy.ACTION_GROUP}],
        },
    }


def experiment_definition(baseline):
    """Temporarily reuse the SAME controller, MI, probes and ETag write guards."""
    definition = copy.deepcopy(baseline)
    actions = definition["actions"]["Access_check_or_alert"]["else"]["actions"][
        "Health_check_or_alert"
    ]["else"]["actions"]
    schema = actions["Parse_alert"]["inputs"]["schema"]
    essentials = schema["properties"]["data"]["properties"]["essentials"]
    essentials["properties"]["alertRule"]["enum"] = [RULE_NAME]
    for name, value in (("signalType", "Metric"), ("monitoringService", "Platform")):
        essentials["required"].append(name)
        essentials["properties"][name] = {"type": "string", "enum": [value]}
    condition = schema["properties"]["data"]["properties"]["alertContext"][
        "properties"
    ]["condition"]["properties"]["allOf"]["items"]
    condition["required"] = ["dimensions", "metricName", "metricNamespace"]
    condition["properties"] = {
        "metricName": {"type": "string", "enum": ["AzureOpenAIRequests"]},
        "metricNamespace": {"type": "string"},
        "dimensions": {
            "type": "array", "minItems": 4, "maxItems": 4, "uniqueItems": True,
            "items": {
                "oneOf": [
                    {
                        "type": "object", "required": ["name", "value"],
                        "properties": {
                            "name": {"type": "string", "enum": [name]},
                            "value": {"type": "string", "enum": [value]},
                        },
                    }
                    for name, value in DIMENSIONS.items()
                ],
            },
        },
    }
    actions["Allowlisted_workspace"] = deploy.guard(
        "@and(equals(toLower(body('Parse_alert')['data']['essentials']"
        "['alertTargetIDs'][0]), toLower('" + ACCOUNT + "')), "
        "equals(toLower(body('Parse_alert')['data']['alertContext']['condition']"
        "['allOf'][0]['metricNamespace']), 'microsoft.cognitiveservices/accounts'))",
        {}, previous="Parse_alert", code="UntrustedMetricSource",
        message="Unexpected Foundry account or metric namespace.",
    )
    actions["Unhealthy_backend"] = deploy.compose("eastus2", previous="Fresh_alert")
    return definition
