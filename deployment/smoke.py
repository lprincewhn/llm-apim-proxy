"""Native Azure OpenAI APIM smoke; synthetic prompts, no route changes or secrets."""

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from azure import APIM, arm
from backends import API_VERSION, load_backends


def read_stream(response):
    content = []
    finish = None
    done = False
    chunks = 0
    for line in response:
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            done = True
            break
        event = json.loads(data)
        chunks += 1
        for choice in event.get("choices", []):
            content.append(choice.get("delta", {}).get("content") or "")
            finish = choice.get("finish_reason") or finish
    return {
        "content": "".join(content), "finish_reason": finish,
        "done": done, "chunks": chunks,
    }


def wildcard_smoke(gateway, key):
    results = []
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        # Deliberately nonexistent resource: never modify real files/jobs/models.
        path = "/proxy-smoke-nonexistent/nested/resource?probe=one&probe=two&encoded=a%2Fb"
        request = urllib.request.Request(
            gateway + path, method=method,
            data=b"unmodified non-JSON payload" if method in ("POST", "PUT", "PATCH") else None,
            headers={"api-key": key, "Content-Type": "application/octet-stream"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=130)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            response.read()
            attempts = response.headers.get("X-Lab-Attempts")
            results.append({
                "case": "wildcard-" + method, "status": response.status,
                "backend": response.headers.get("X-Lab-Backend"), "attempts": attempts,
                "pass": response.status in (404, 405) and attempts == "1",
            })
    return results


def run():
    gateway = arm("GET", APIM)["properties"]["gatewayUrl"]
    key = arm("POST", APIM + "/subscriptions/lab-validation/listSecrets", {})["primaryKey"]
    backends = load_backends()
    primary = json.loads(arm("GET", APIM + "/namedValues/chat-route")["properties"]["value"])["primary"]
    chat = {
        "messages": [{"role": "user", "content": "Reply only OK"}],
        "max_completion_tokens": 32, "reasoning_effort": "none",
    }
    cases = [
        ("chat-eastus2-alias", "eastus2", chat, API_VERSION, 200),
        ("chat-sweden-alias", "sweden", chat, API_VERSION, 200),
        ("chat-stream", "eastus2", {**chat, "stream": True}, API_VERSION, 200),
        ("embedding", "embedding", {"input": "synthetic connectivity probe"}, API_VERSION, 200),
        ("invalid-version", "eastus2", chat, "invalid-version", 404),
        ("invalid-body", "eastus2", {"messages": "not-an-array"}, API_VERSION, 400),
        ("v1-chat", "v1", {**chat, "model": backends[primary]["deployment"]}, API_VERSION, 200),
        ("v1-responses", "responses", {
            "model": backends[primary]["deployment"], "input": "Reply only OK",
            "max_output_tokens": 32, "reasoning": {"effort": "none"}, "store": False,
        }, API_VERSION, 200),
    ]
    results = []
    for name, target, body, version, expected in cases:
        operation = "embeddings" if target == "embedding" else "chat/completions"
        url = (gateway + "/openai/v1/responses" if target == "responses" else
               gateway + "/openai/v1/chat/completions" if target == "v1" else
               gateway + "/openai/deployments/" + backends[target]["deployment"]
               + "/" + operation + "?api-version=" + version)
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "api-key": key},
        )
        start = time.monotonic()
        try:
            response = urllib.request.urlopen(request, timeout=130)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result = {
                "case": name, "status": response.status,
                "backend": response.headers.get("X-Lab-Backend"),
                "attempts": response.headers.get("X-Lab-Attempts"),
                "content_type": response.headers.get("Content-Type"),
            }
            valid = False
            if body.get("stream") and response.status == 200:
                result.update(read_stream(response))
                valid = (result["content"] == "OK" and result["finish_reason"] == "stop"
                         and result["done"] and result["chunks"] > 0
                         and "text/event-stream" in (result["content_type"] or ""))
            else:
                try:
                    data = json.load(response)
                except json.JSONDecodeError:
                    result["response_error"] = "Expected JSON response"
                    result["elapsed_ms"] = round((time.monotonic() - start) * 1000)
                    result["pass"] = False
                    results.append(result)
                    continue
                if expected >= 400:
                    valid = response.status == expected and isinstance(data.get("error"), dict)
                    result["error_code"] = data.get("error", {}).get("code")
                elif response.status == 200 and target == "embedding":
                    vector = data.get("data", [{}])[0].get("embedding", [])
                    result["dimensions"] = len(vector)
                    valid = len(vector) == 1536 and all(
                        isinstance(value, (int, float)) and math.isfinite(value) for value in vector
                    )
                elif response.status == 200 and target == "responses":
                    result["content"] = "".join(
                        part["text"] for item in data.get("output", [])
                        if item.get("type") == "message"
                        for part in item.get("content", []) if part.get("type") == "output_text"
                    )
                    result["response_status"] = data.get("status")
                    valid = result["content"] == "OK" and result["response_status"] == "completed"
                elif response.status == 200:
                    choice = data.get("choices", [{}])[0]
                    result["content"] = choice.get("message", {}).get("content")
                    result["finish_reason"] = choice.get("finish_reason")
                    valid = result["content"] == "OK" and result["finish_reason"] == "stop"
            result["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            result["pass"] = response.status == expected and valid and result["attempts"] == "1"
            results.append(result)
    results.extend(wildcard_smoke(gateway, key))
    return {"completed_at": datetime.now(timezone.utc).isoformat(), "results": results}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if all(item["pass"] for item in report["results"]) else 1)
