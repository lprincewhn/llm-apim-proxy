"""Bounded fault deadlines and configuration-switch evidence for this lab."""

import json
import time
from pathlib import Path

from azure import APIM, APP, arm, container_secrets
from validate import post

gateway = arm("GET", APIM)["properties"]["gatewayUrl"]
subkey = arm("POST", APIM + "/subscriptions/lab-validation/listSecrets", {})["primaryKey"]
headers = {"Ocp-Apim-Subscription-Key": subkey}
app = arm("GET", APP, version="2024-03-01")
executor = "https://" + app["properties"]["configuration"]["ingress"]["fqdn"]
private_headers = {
    "X-Executor-Key": container_secrets()["executor-key"],
    "X-Budget-Ms": "2000", "X-Idle-Ms": "500",
}
results = []
for scenario, status, code in (
    ("body-stall", 504, "body_read_timeout"),
    ("drip", 504, "total_timeout"),
    ("delayed-headers", 504, "total_timeout"),
    ("truncated", 502, "invalid_upstream_json"),
    ("oversized", 502, "response_too_large"),
):
    result = post(executor + "/fault/" + scenario, private_headers, {})
    result.update(test="executor_" + scenario)
    result["pass"] = result["status"] == status and result["error"] == code and result["elapsed_ms"] < 4000
    results.append(result)
result = post(executor + "/fault/success", {}, {})
result.update(test="executor_requires_auth", **{"pass": result["status"] == 401})
results.append(result)
for backend in ("eastus2", "sweden", "embedding"):
    body = {"input": "synthetic non-sensitive test"} if backend == "embedding" else {
        "messages": [{"role": "user", "content": "Reply only OK"}],
        "max_completion_tokens": 32, "reasoning_effort": "none",
    }
    result = post(executor + "/execute/" + backend,
                  {**private_headers, "X-Budget-Ms": "5500"}, body)
    result.update(test="managed_identity_" + backend)
    result["pass"] = result["status"] == 200
    results.append(result)

# Test real configuration propagation, not a monitoring-alert simulation.
route_id = APIM + "/namedValues/chat-route"
original = arm("GET", route_id)["properties"]
replacement = {**original, "value": json.dumps({
    "primary": "sweden", "enabled": ["sweden"], "version": 2,
})}
try:
    arm("PUT", route_id, {"properties": replacement})
    deadline = time.monotonic() + 180
    while True:
        result = post(gateway + "/validation/success", headers, {})
        if result["status"] == 200 and result["backend"] == "sweden":
            result.update(test="config_switch_to_sweden", **{"pass": True})
            results.append(result)
            break
        if time.monotonic() >= deadline:
            result.update(test="config_switch_to_sweden", **{"pass": False})
            results.append(result)
            break
        time.sleep(10)
finally:
    arm("PUT", route_id, {"properties": original})
deadline = time.monotonic() + 180
while True:
    result = post(gateway + "/validation/success", headers, {})
    if result["status"] == 200 and result["backend"] == "eastus2":
        result.update(test="config_restore_eastus2", **{"pass": True})
        results.append(result)
        break
    if time.monotonic() >= deadline:
        result.update(test="config_restore_eastus2", **{"pass": False})
        results.append(result)
        break
    time.sleep(10)
Path(__file__).with_name("extended-results.json").write_text(
    json.dumps(results, indent=2), encoding="utf-8")
print(json.dumps(results, indent=2))
