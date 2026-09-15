# Buffered Azure OpenAI executor

Python 3.11+; the supplied container uses `python:3.12-slim`, runs as a non-root
user, exposes port 8000 and starts `python app.py`. Build with this directory
as the context: `docker build -t buffered-executor .`. `.dockerignore` excludes
everything except the explicit runtime/build files, including local secrets,
tests and virtual environments.

Current hosting target: the isolated Azure Container Apps lab in eastus2, with
an attached user-assigned managed identity and minimum replicas 1. Configure
ingress target port 8000 and HTTP readiness/liveness probes on `/health`.
Minimum replicas avoids scale-to-zero cold starts but incurs running cost;
configure an explicit maximum replica count and resource limits for the lab.
Provisioning, ACR publishing and deployment are handled separately.

For non-container hosting, install `requirements.txt` and run `python app.py`.
App Service ZIP deployment remains supported by placing the three Python
modules and `requirements.txt` at the ZIP root and enabling dependency build.

Required application settings:

- `EXECUTOR_KEY`: high-entropy shared secret, at least 32 bytes; distribute via
  a secret store, never in source or logs.
- `BACKENDS_JSON`: fixed routing map, for example
  `{"chat-primary":{"endpoint":"https://YOUR-RESOURCE.openai.azure.com","deployment":"YOUR-DEPLOYMENT","kind":"chat"}}`.
  Kinds are `chat` and `embedding`. Only Azure HTTPS origins are accepted;
  paths, user info, query strings and arbitrary caller routing are forbidden.
  A single backend is valid; this service does not assume three real endpoints
  or create model deployments. Configure only confirmed authorized deployments.
- Optional per-backend `api_key_env`: the **name** of an environment variable
  containing that resource's Azure OpenAI API key. Store the value as a
  Container App secret and inject it through a secret-backed environment
  variable, never as a literal inside `BACKENDS_JSON`, code or the image.
  Missing, empty or invalid referenced values fail startup explicitly.
  When this field is present, the backend uses only the `api-key` header;
  it does not request a managed identity token or fall back to another
  authentication method. Without it, managed identity remains the default.
  Keys are loaded at startup; restart/update the revision after rotation.
  This optional mode requires the resource owner to permit local authentication;
  it is not a workaround for disabled local authentication or missing RBAC.
- Optional `AZURE_CLIENT_ID`: user-assigned managed identity client ID;
  otherwise uses system-assigned identity. Assign its principal Cognitive
  Services OpenAI User access to resources using managed identity authentication.
  For Container Apps, attach that same user-assigned identity and set its
  **client ID** here, not the resource ID or principal/object ID.
- `PORT`: default `8000`.
- `ENABLE_FAULTS`: default `false`; enable only in the authorized test environment.

The deployment owner's confirmed lab map uses **managed identity only**.
All entries deliberately omit `api_key_env`. The existing eastus2 resource has
local authentication disabled; do not change that security setting or attempt
key authentication there. Live Foundry invocation remains blocked until the
resource owner grants the attached user-assigned identity resource-scoped
**Cognitive Services OpenAI User** access. Deterministic loopback faults remain
available without these grants when explicitly enabled for the lab.

```json
{
  "eastus2": {
    "endpoint": "https://svhw-openai-eastus2.openai.azure.com/",
    "deployment": "gpt-5.1",
    "kind": "chat"
  },
  "sweden": {
    "endpoint": "https://svhw2-swedencentral.openai.azure.com/",
    "deployment": "svhwb107-gpt51",
    "kind": "chat"
  },
  "embedding": {
    "endpoint": "https://svhw2-westus3.openai.azure.com/",
    "deployment": "text-embedding-3-small",
    "kind": "embedding"
  }
}
```

`GET /health` is unauthenticated and does not contact Azure. **Every other
endpoint** requires `X-Executor-Key`. `POST /execute/{backendId}` accepts a JSON
object. An omitted `model` is filled with the fixed deployment name; a supplied
model must match it. Only `stream: false` or an omitted stream is accepted.
Chat parameters such as `max_completion_tokens` are forwarded without SDK
translation, using stable Azure OpenAI API version `2024-10-21`; gpt-5.1
deployment/parameter compatibility must be verified for the chosen resource.
The executor does not retry or redirect requests, use ambient proxies, or
forward client authentication headers. One shared async managed identity
credential supplies cached Cognitive Services tokens for MI-authenticated
backends; explicitly configured API-key backends bypass token acquisition.

`X-Budget-Ms` defaults to and is capped at **5500 ms** (minimum 1 ms).
It covers incoming body reading, token acquisition, HTTP connection/headers,
full body reads and JSON validation. `X-Idle-Ms` defaults to **1200 ms**,
clamped to 1–5500 ms; this is a per-read body idle limit, not an additional
overall deadline. Invalid timeout headers return 400. Request and response
bodies are bounded to 2 MiB; only 16 requests run simultaneously (excess: fast
503). Compressed bodies are rejected, and requests cancel on client disconnect.

No downstream response is prepared until the complete upstream body is received
and validated as JSON. Valid JSON preserves the upstream status, including
400/401/403/429/5xx, and selected retry/request-ID headers. Malformed or oversized
upstream bodies return 502. Idle timeout returns 504 with
`x-executor-error: body_read_timeout`; total timeout uses `total_timeout`.
Connection failures return 502; identity failures return 503. Error details,
keys, bearer tokens, prompts, outputs and full URLs are never logged. Structured
logs contain generated attempt ID, configured backend ID, status, elapsed time
and phase. JSON parsing is bounded synchronous work; elapsed time is checked
after it, so this is an application deadline, not a hard real-time guarantee.

## Deterministic faults and tests (no Foundry calls)

With faults enabled, authenticated `POST /fault/{scenario}` uses **the same
buffered HTTP reader and deadline** against a separate ephemeral loopback-only
HTTP server. The loopback listener additionally uses an independent random
internal key, never the executor key or an Azure token. No managed identity
token is requested for faults. Supported scenarios:

`success`, `body-stall`, `drip`, `delayed-headers`, `truncated`, `oversized`,
`status400`, `status401`, `status403`, `status429`, `status500`, `status503`.

Success emits two chunks 75 ms apart; body-stall sends headers and an initial
JSON fragment before stalling 10 seconds; drip sends a byte every 25 ms for
10 seconds; delayed-headers waits 10 seconds. Short budgets/idle headers make
tests fast. `truncated` sends a completed HTTP body containing unfinished JSON.
Synthetic failure statuses have valid JSON and `retry-after: 2`.

Run from this directory:

```sh
python -m unittest discover -s tests -v
```
