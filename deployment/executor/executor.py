"""Bounded, single-attempt Azure OpenAI HTTP execution."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BUDGET_MS = 5500
DEFAULT_IDLE_MS = 1200
MAX_CONCURRENCY = 16
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
API_VERSION = "2024-10-21"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
RESPONSE_HEADERS = (
    "retry-after",
    "retry-after-ms",
    "x-ms-retry-after-ms",
    "apim-request-id",
    "x-request-id",
    "x-ms-request-id",
    "request-id",
)


class ExecutorFailure(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON number")


def parse_json(body: bytes):
    return json.loads(body, parse_constant=_reject_constant)


@dataclass(frozen=True)
class Backend:
    endpoint: str
    deployment: str
    kind: str
    api_key_env: str | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def url(self) -> str:
        operation = "chat/completions" if self.kind == "chat" else "embeddings"
        return (
            f"{self.endpoint}/openai/deployments/{self.deployment}/"
            f"{operation}?api-version={API_VERSION}"
        )


@dataclass(frozen=True)
class Settings:
    executor_key: str = field(repr=False)
    backends: dict[str, Backend]
    identity_client_id: str | None = None

    @classmethod
    def from_env(cls, env):
        key = env.get("EXECUTOR_KEY", "")
        if len(key.encode("utf-8")) < 32:
            raise ValueError("EXECUTOR_KEY must contain at least 32 bytes")
        try:
            raw = parse_json(env.get("BACKENDS_JSON", "{}").encode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("BACKENDS_JSON must be a JSON object") from None
        if not isinstance(raw, dict):
            raise ValueError("BACKENDS_JSON must be a JSON object")
        backends = {}
        for name, value in raw.items():
            if not SAFE_NAME.fullmatch(name) or not isinstance(value, dict):
                raise ValueError("Invalid backend definition")
            required = {"endpoint", "deployment", "kind"}
            if not required.issubset(value) or set(value) - required - {"api_key_env"}:
                raise ValueError("Backend requires endpoint, deployment, kind; api_key_env is optional")
            endpoint, deployment, kind = (
                value["endpoint"], value["deployment"], value["kind"]
            )
            if not isinstance(endpoint, str):
                raise ValueError("Invalid trusted backend endpoint")
            try:
                url = urlsplit(endpoint)
                valid = (
                    url.scheme == "https"
                    and url.hostname is not None
                    and re.fullmatch(r"[a-z0-9.-]+", url.hostname) is not None
                    and any(url.hostname.endswith(suffix) for suffix in (
                        ".openai.azure.com",
                        ".cognitiveservices.azure.com",
                        ".services.ai.azure.com",
                    ))
                    and url.port in (None, 443)
                    and url.username is None
                    and url.password is None
                    and url.path in ("", "/")
                    and not url.query
                    and not url.fragment
                    and not any(char.isspace() for char in endpoint)
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Backend endpoint must be a trusted Azure HTTPS origin")
            if (
                not isinstance(deployment, str)
                or not SAFE_NAME.fullmatch(deployment)
                or kind not in ("chat", "embedding")
            ):
                raise ValueError("Invalid backend deployment or kind")
            api_key_env = None
            api_key = None
            if "api_key_env" in value:
                api_key_env = value["api_key_env"]
                if not isinstance(api_key_env, str) or not ENV_NAME.fullmatch(api_key_env):
                    raise ValueError("Invalid API key environment variable name")
                api_key = env.get(api_key_env)
                if (
                    not isinstance(api_key, str)
                    or not 1 <= len(api_key) <= 4096
                    or any(not 33 <= ord(char) <= 126 for char in api_key)
                ):
                    raise ValueError(f"API key environment variable {api_key_env} is missing or invalid")
            backends[name] = Backend(endpoint.rstrip("/"), deployment, kind, api_key_env, api_key)
        if not backends:
            raise ValueError("At least one backend is required")
        return cls(key, backends, env.get("AZURE_CLIENT_ID") or None)


@dataclass
class Attempt:
    attempt_id: str
    deadline: float
    backend: str = "unselected"
    phase: str = "admission"


@dataclass
class BufferedResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def deadline_check(attempt: Attempt):
    # JSON parsing is synchronous: also check the clock after bounded CPU work.
    if asyncio.get_running_loop().time() >= attempt.deadline:
        raise TimeoutError


def bounded_ms(headers, name: str, default: int, maximum: int) -> int:
    raw = headers.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ExecutorFailure(400, "invalid_timeout_header") from None
    return min(max(value, 1), maximum)


def validate_payload(body: bytes, backend: Backend) -> bytes:
    try:
        value = parse_json(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutorFailure(400, "invalid_request_json") from None
    if not isinstance(value, dict):
        raise ExecutorFailure(400, "request_must_be_object")
    if any(name in value for name in (
        "url", "endpoint", "base_url", "api_base", "api_version",
        "deployment", "backend", "backendId", "api_key", "api_key_env",
    )):
        raise ExecutorFailure(400, "routing_override_forbidden")
    if "model" in value and value["model"] != backend.deployment:
        raise ExecutorFailure(400, "model_mismatch")
    if "stream" in value and value["stream"] is not False:
        raise ExecutorFailure(400, "streaming_not_supported")
    value["model"] = backend.deployment
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutorFailure(400, "invalid_request_json") from None
    if len(encoded) > MAX_BODY_BYTES:
        raise ExecutorFailure(413, "request_too_large")
    return encoded


async def buffered_post(
    session: aiohttp.ClientSession,
    url: str,
    body: bytes,
    headers: dict[str, str],
    idle_ms: int,
    attempt: Attempt,
) -> BufferedResponse:
    """Called inside the attempt's total deadline; never writes downstream."""
    attempt.phase = "response_headers"
    try:
        # No redirects, proxy environment, decompression, or retry. In particular,
        # a redirect must never carry a bearer credential to another endpoint.
        async with session.post(
            url,
            data=body,
            headers={
                **headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
        ) as response:
            attempt.phase = "body_read"
            if response.content_length is not None and response.content_length > MAX_BODY_BYTES:
                raise ExecutorFailure(502, "response_too_large")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ExecutorFailure(502, "unsupported_content_encoding")
            data = bytearray()
            while True:
                try:
                    async with asyncio.timeout(idle_ms / 1000):
                        chunk = await response.content.read(64 * 1024)
                except TimeoutError:
                    raise ExecutorFailure(504, "body_read_timeout") from None
                if not chunk:
                    break
                if len(data) + len(chunk) > MAX_BODY_BYTES:
                    raise ExecutorFailure(502, "response_too_large")
                data.extend(chunk)
                deadline_check(attempt)
            attempt.phase = "json_validation"
            deadline_check(attempt)
            try:
                parse_json(data)
            except (ValueError, UnicodeError, RecursionError):
                raise ExecutorFailure(502, "invalid_upstream_json") from None
            deadline_check(attempt)
            return BufferedResponse(
                response.status,
                bytes(data),
                {name: response.headers[name] for name in RESPONSE_HEADERS
                 if name in response.headers},
            )
    except aiohttp.ClientPayloadError:
        raise ExecutorFailure(502, "upstream_body_error") from None
    except aiohttp.ClientConnectionError:
        raise ExecutorFailure(502, "connection_error") from None
    except aiohttp.ClientResponseError:
        raise ExecutorFailure(502, "upstream_protocol_error") from None
