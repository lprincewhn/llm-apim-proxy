"""Small, bounded live validation. Does not print subscription credentials."""

import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

from azure import APIM, APP, arm, container_secrets


def post(url, headers, body, timeout=10):
    start = time.monotonic()
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "Content-Type": "application/json", **headers,
    })
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return {
            "status": response.status,
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "attempts": response.headers.get("X-Lab-Attempts"),
            "backend": response.headers.get("X-Lab-Backend"),
            "error": response.headers.get("x-executor-error"),
            "policy_error": response.headers.get("X-Lab-Error-Reason"),
            "policy_source": response.headers.get("X-Lab-Error-Source"),
            "policy_path": response.headers.get("X-Lab-Error-Path"),
            "policy_detail": response.headers.get("X-Lab-Error-Detail"),
            "valid_json": _json_valid(raw),
        }


def _json_valid(raw):
    try:
        json.loads(raw)
        return True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def main():
    apim = arm("GET", APIM)
    gateway = apim["properties"]["gatewayUrl"]
    key = arm("POST", APIM + "/subscriptions/lab-validation/listSecrets", {})["primaryKey"]
    headers = {"Ocp-Apim-Subscription-Key": key}
    results = []
    for scenario, expected, attempts in (
        ("success", 200, 1),
        ("body-stall", 200, 2),
        ("status400", 400, 1),
        ("status401", 401, 1),
        ("status403", 403, 1),
        ("status429", 200, 2),
        ("status500", 200, 2),
        ("status503", 200, 2),
    ):
        result = post(gateway + "/validation/" + scenario, headers, {})
        result.update(test=scenario, expected_status=expected, expected_attempts=attempts)
        result["pass"] = result["status"] == expected and result["attempts"] == str(attempts) and result["elapsed_ms"] < 4500
        results.append(result)
    denied = post(gateway + "/llm/generate", {}, {})
    denied.update(test="missing_subscription", **{"pass": denied["status"] == 401})
    results.append(denied)
    body = {
        "model": "gpt-5.1",
        "messages": [{"role": "user", "content": "Reply only: OK"}],
        "max_completion_tokens": 32, "reasoning_effort": "none",
    }
    for purpose in ("generate", "rewrite", "intent"):
        result = post(gateway + "/llm/" + purpose, headers, body)
        result.update(test="real_foundry_" + purpose, **{"pass": result["status"] == 200})
        results.append(result)
    result = post(gateway + "/llm/embedding", headers, {
        "model": "text-embedding-3-small", "input": "customer service test",
    })
    result.update(test="real_embedding", **{"pass": result["status"] == 200})
    results.append(result)
    Path(__file__).with_name("validation-results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
