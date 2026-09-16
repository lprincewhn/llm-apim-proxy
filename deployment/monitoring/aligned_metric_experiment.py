"""Compute the APIM error threshold from Foundry status-code metric series.

Opt-in experiment only. No Azure calls on import. The foreground collector
publishes derived metrics; Azure Monitor, not the collector, invokes failover.
"""

import copy
from datetime import datetime, timedelta, timezone
import math
import urllib.error
import urllib.parse
import urllib.request
import json

import deploy
import metric_experiment
from azure import arm


ACCOUNT = metric_experiment.ACCOUNT
RULE_NAME = "svhwb124-foundry-error-rate"
RULE = deploy.ROOT + "/providers/Microsoft.Insights/metricAlerts/" + RULE_NAME
METRIC_API = metric_experiment.METRIC_API
TELEMETRY = deploy.ROOT + "/providers/Microsoft.Insights/components/svhwb124-metric-math"
TELEMETRY_API = "2020-02-02"
NAMESPACE = "Azure.ApplicationInsights"
OPERATIONS = ("chatcompletions_create", "responses_create")
DIMENSIONS = {"Backend": "eastus2"}
CONNECTION = None


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def source_window(at):
    end = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return end - timedelta(minutes=5), end


def query(at):
    start, end = source_window(at)
    params = urllib.parse.urlencode({
        "metricnames": "AzureOpenAIRequests",
        "timespan": start.isoformat().replace("+00:00", "Z") + "/"
        + end.isoformat().replace("+00:00", "Z"),
        "interval": "PT1M", "aggregation": "Total", "top": 1000,
        "$filter": "ApiName eq 'OpenAI' and ModelDeploymentName eq 'gpt-5.1' "
        "and StatusCode eq '*' and OperationName eq '*'",
    })
    return arm(
        "GET", ACCOUNT + "/providers/Microsoft.Insights/metrics",
        version="2023-10-01&" + params,
    )


def calculate(payload, at):
    """Sum eligible status series, never average per-status percentages."""
    start, end = source_window(at)
    if payload.get("nextLink"):
        raise ValueError("Truncated metric response")
    values = payload.get("value", [])
    if len(values) != 1 or values[0].get("name", {}).get("value") != "AzureOpenAIRequests":
        raise ValueError("Unexpected metric response")
    metric = values[0]
    if metric.get("errorCode", "Success") != "Success":
        raise ValueError("Azure reported a metric-query error")
    series = metric.get("timeseries", [])
    if len(series) >= 1000:
        raise ValueError("Metric series limit reached; denominator may be incomplete")
    totals = {}
    seen = set()
    datapoints = 0
    for item in series:
        dims = {entry["name"]["value"].lower(): entry["value"] for entry in item["metadatavalues"]}
        if len(dims) != len(item["metadatavalues"]):
            raise ValueError("Duplicate metric dimension")
        if dims.get("apiname") != "OpenAI" or dims.get("modeldeploymentname") != "gpt-5.1":
            raise ValueError("Unexpected source attribution")
        if dims.get("operationname") not in OPERATIONS:
            continue
        key = tuple(sorted(dims.items()))
        if key in seen:
            raise ValueError("Duplicate metric series")
        seen.add(key)
        status = dims.get("statuscode", "")
        if not status.isdecimal() or not 100 <= int(status) <= 599:
            raise ValueError("Unrecognized status code; cannot align denominator")
        for point in item.get("data", []):
            timestamp = parse_time(point["timeStamp"])
            if not start <= timestamp < end:
                raise ValueError("Metric point outside requested five-minute window")
            value = point.get("total")
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid metric total")
            datapoints += 1
            totals[status] = totals.get(status, 0) + value
    if not datapoints:
        raise ValueError("No metric datapoints; missing telemetry is not healthy")
    samples = sum(value for status, value in totals.items() if status not in ("401", "403"))
    errors = sum(
        value for status, value in totals.items()
        if int(status) in (404, 429) or 500 <= int(status) <= 599
    )
    rate = 100 * errors / samples if samples else None
    return {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "by_status": totals, "eligible_samples": samples, "errors": errors,
        "error_rate_pct": rate,
        "violation": int(samples >= 5 and errors * 5 >= samples),
    }


def publish(result, at):
    global CONNECTION
    if CONNECTION is None:
        component = arm("GET", TELEMETRY, version=TELEMETRY_API)
        CONNECTION = dict(
            part.split("=", 1) for part in component["properties"]["ConnectionString"].split(";") if part
        )
    endpoint = urllib.parse.urlsplit(CONNECTION["IngestionEndpoint"])
    if endpoint.scheme != "https" or not endpoint.hostname \
            or not endpoint.hostname.endswith(".in.applicationinsights.azure.com"):
        raise ValueError("Unexpected Application Insights ingestion endpoint")
    metrics = {
        "EligibleSamples": result["eligible_samples"],
        "ErrorCount": result["errors"], "ViolationCount": result["violation"],
    }
    if result["error_rate_pct"] is not None:
        metrics["ErrorRatePct"] = result["error_rate_pct"]
    for name, value in metrics.items():
        body = {
            "name": "Microsoft.ApplicationInsights.Metric", "time": at.isoformat(),
            "iKey": CONNECTION["InstrumentationKey"],
            "data": {"baseType": "MetricData", "baseData": {
                "ver": 2,
                "metrics": [{"name": name, "kind": "Aggregation", "value": value,
                             "count": 1, "min": value, "max": value}],
                "properties": {"Backend": "eastus2", "Source": "FoundryStatusMetricMath"},
            }},
        }
        request = urllib.request.Request(
            CONNECTION["IngestionEndpoint"].rstrip("/") + "/v2/track",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 202):
                    raise RuntimeError("Custom metric emission was not accepted")
                receipt = json.load(response)
                if receipt.get("itemsAccepted") != 1 or receipt.get("errors"):
                    raise RuntimeError("Application Insights rejected calculated telemetry")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Custom metric emission failed: HTTP {exc.code}") from None


def metric_rule(enabled=False, *, preflight=False):
    rule = metric_experiment.metric_rule(enabled)
    rule["properties"].update(
        description="SVHWB-124 aligned Foundry: (404+429+5xx)/(all-401-403)>=20%, N>=5, rolling5m.",
        windowSize="PT1M",
        scopes=[TELEMETRY],
    )
    criterion = rule["properties"]["criteria"]["allOf"][0]
    criterion.update(
        name="ComputedAlignedErrorRate", metricNamespace=NAMESPACE,
        metricName="EligibleSamples" if preflight else "ViolationCount",
        timeAggregation="Maximum", threshold=5 if preflight else 1,
        dimensions=[],
        skipMetricValidation=False,
    )
    return rule


def experiment_definition(baseline, *, preflight=False):
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
    condition["required"].extend(["metricName", "metricNamespace"])
    condition["properties"].update({
        "metricName": {"type": "string", "enum": ["EligibleSamples" if preflight else "ViolationCount"]},
        "metricNamespace": {"type": "string", "enum": [NAMESPACE, NAMESPACE.lower()]},
    })
    condition["properties"]["dimensions"] = {"type": "array", "minItems": 0, "maxItems": 0}
    actions["Allowlisted_workspace"] = deploy.guard(
        "@equals(toLower(body('Parse_alert')['data']['essentials']['alertTargetIDs'][0]), "
        "toLower('" + TELEMETRY + "'))",
        {}, previous="Parse_alert", code="UntrustedMetricSource",
        message="Unexpected calculated-metric resource.",
    )
    actions["Unhealthy_backend"] = deploy.compose("eastus2", previous="Fresh_alert")
    return definition
