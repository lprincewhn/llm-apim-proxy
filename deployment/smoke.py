"""Small direct-Foundry APIM smoke calls; no route changes or credential output."""

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from azure import APIM, arm


def run():
    gateway = arm("GET", APIM)["properties"]["gatewayUrl"]
    key = arm("POST", APIM + "/subscriptions/lab-validation/listSecrets", {})["primaryKey"]
    results = []
    for purpose in ("intent", "rewrite", "generate", "embedding"):
        embedding = purpose == "embedding"
        body = {"model": "text-embedding-3-small", "input": "synthetic connectivity probe"} if embedding else {
            "model": "gpt-5.1",
            "messages": [{"role": "user", "content": "Reply only OK"}],
            "max_completion_tokens": 32, "reasoning_effort": "none",
        }
        request = urllib.request.Request(
            gateway + "/llm/" + purpose, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Ocp-Apim-Subscription-Key": key},
        )
        start = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            data = json.load(response)
            result = {
                "purpose": purpose, "status": response.status,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "backend": response.headers.get("X-Lab-Backend"),
                "attempts": response.headers.get("X-Lab-Attempts"),
                "model": data.get("model"),
            }
            valid = False
            if response.status == 200:
                if embedding:
                    vector = data.get("data", [{}])[0].get("embedding", [])
                    result["dimensions"] = len(vector)
                    valid = len(vector) == 1536 and all(
                        isinstance(value, (int, float)) and math.isfinite(value) for value in vector
                    )
                else:
                    choice = data.get("choices", [{}])[0]
                    result["content"] = choice.get("message", {}).get("content")
                    result["finish_reason"] = choice.get("finish_reason")
                    valid = result["content"] == "OK" and result["finish_reason"] == "stop"
            else:
                error = data.get("error", {})
                result["error_code"] = error.get("code") if isinstance(error, dict) else None
                result["gateway_error_reason"] = response.headers.get("X-Lab-Error-Reason")
                result["gateway_error_source"] = response.headers.get("X-Lab-Error-Source")
            result["pass"] = response.status == 200 and valid and result["attempts"] == "1"
            results.append(result)
    return {"completed_at": datetime.now(timezone.utc).isoformat(), "results": results}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if all(item["pass"] for item in report["results"]) else 1)
