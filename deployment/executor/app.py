"""Run with Python 3.11+: python app.py."""

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass
from uuid import uuid4

import aiohttp
from aiohttp import web
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import ManagedIdentityCredential

from executor import (
    DEFAULT_IDLE_MS,
    MAX_BODY_BYTES,
    MAX_BUDGET_MS,
    MAX_CONCURRENCY,
    TOKEN_SCOPE,
    Attempt,
    ExecutorFailure,
    Settings,
    bounded_ms,
    buffered_post,
    deadline_check,
    validate_payload,
)

LOG = logging.getLogger("executor")


@dataclass
class Runtime:
    settings: Settings
    credential: object = None
    session: aiohttp.ClientSession | None = None
    active: int = 0


RUNTIME = web.AppKey("runtime", Runtime)
ATTEMPT = web.RequestKey("attempt", Attempt)
IDLE = web.RequestKey("idle_ms", int)


def error_response(status: int, code: str) -> web.Response:
    response = web.json_response(
        {"error": {"code": code}},
        status=status,
        headers={"x-executor-error": code, "Cache-Control": "no-store"},
    )
    # Rejecting an unread body must not leave slow/unbounded draining work.
    response.force_close()
    return response


@web.middleware
async def guard(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)
    runtime = request.app[RUNTIME]
    loop = asyncio.get_running_loop()
    started = loop.time()
    attempt = Attempt(uuid4().hex, started + MAX_BUDGET_MS / 1000)
    request[ATTEMPT] = attempt
    admitted = False
    status = 500
    try:
        supplied = request.headers.get("X-Executor-Key", "")
        if not secrets.compare_digest(
            supplied.encode("utf-8", "surrogateescape"),
            runtime.settings.executor_key.encode("utf-8"),
        ):
            raise ExecutorFailure(401, "unauthorized")
        budget_ms = bounded_ms(request.headers, "X-Budget-Ms", MAX_BUDGET_MS, MAX_BUDGET_MS)
        request[IDLE] = bounded_ms(request.headers, "X-Idle-Ms", DEFAULT_IDLE_MS, MAX_BUDGET_MS)
        attempt.deadline = started + budget_ms / 1000
        if runtime.active >= MAX_CONCURRENCY:
            raise ExecutorFailure(503, "capacity_exceeded")
        # There is no await between the limit check and increment.
        runtime.active += 1
        admitted = True
        async with asyncio.timeout_at(attempt.deadline):
            response = await handler(request)
            deadline_check(attempt)
        status = response.status
        return response
    except ExecutorFailure as exc:
        status = exc.status
        return error_response(exc.status, exc.code)
    except TimeoutError:
        status = 504
        return error_response(504, "total_timeout")
    except (ClientAuthenticationError, ServiceRequestError, ServiceResponseError):
        status = 503
        return error_response(503, "identity_unavailable")
    except web.HTTPRequestEntityTooLarge:
        status = 413
        return error_response(413, "request_too_large")
    except web.HTTPException as exc:
        status = exc.status
        return error_response(exc.status, "http_error")
    except asyncio.CancelledError:
        status = 499
        raise
    finally:
        if admitted:
            runtime.active -= 1
        LOG.info(json.dumps({
            "attempt_id": attempt.attempt_id,
            "backend": attempt.backend,
            "status": status,
            "elapsed_ms": round((loop.time() - started) * 1000, 1),
            "phase": attempt.phase,
        }))


async def request_body(request: web.Request) -> bytes:
    request[ATTEMPT].phase = "request_read"
    if request.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ExecutorFailure(400, "unsupported_request_encoding")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise ExecutorFailure(413, "request_too_large")
    body = bytearray()
    while chunk := await request.content.read(64 * 1024):
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ExecutorFailure(413, "request_too_large")
        body.extend(chunk)
        deadline_check(request[ATTEMPT])
    return bytes(body)


def committed_response(result, attempt: Attempt) -> web.Response:
    deadline_check(attempt)
    attempt.phase = "complete"
    return web.Response(
        body=result.body,
        status=result.status,
        headers={
            **result.headers,
            "x-executor-attempt-id": attempt.attempt_id,
            "Cache-Control": "no-store",
        },
        content_type="application/json",
    )


async def execute(request: web.Request):
    runtime = request.app[RUNTIME]
    attempt = request[ATTEMPT]
    backend_id = request.match_info["backend_id"]
    backend = runtime.settings.backends.get(backend_id)
    if backend is None:
        raise ExecutorFailure(404, "unknown_backend")
    attempt.backend = backend_id
    body = await request_body(request)
    attempt.phase = "request_validation"
    body = validate_payload(body, backend)
    deadline_check(attempt)
    attempt.phase = "credential"
    if backend.api_key_env is None:
        token = await runtime.credential.get_token(TOKEN_SCOPE)
        upstream_headers = {"Authorization": f"Bearer {token.token}"}
    else:
        upstream_headers = {"api-key": backend.api_key}
    deadline_check(attempt)
    result = await buffered_post(
        runtime.session, backend.url, body,
        upstream_headers,
        request[IDLE], attempt,
    )
    return committed_response(result, attempt)


async def health(request: web.Request):
    return web.json_response({"status": "ok"})


async def lifecycle(app: web.Application):
    runtime = app[RUNTIME]
    credential = runtime.credential
    if credential is None:
        credential = ManagedIdentityCredential(
            client_id=runtime.settings.identity_client_id,
            retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
            logging_enable=False,
        )
    runtime.credential = credential
    async with credential:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=MAX_CONCURRENCY, limit_per_host=MAX_CONCURRENCY),
            timeout=aiohttp.ClientTimeout(total=None),
            auto_decompress=False,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            runtime.session = session
            yield


def create_app(settings: Settings | None = None, credential=None) -> web.Application:
    app = web.Application(
        middlewares=[guard],
        client_max_size=MAX_BODY_BYTES,
        handler_args={
            "handler_cancellation": True,
            "auto_decompress": False,
            "lingering_time": 0,
        },
    )
    app[RUNTIME] = Runtime(settings or Settings.from_env(os.environ), credential)
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", health)
    app.router.add_post("/execute/{backend_id}", execute)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOG.setLevel(logging.INFO)
    # SDK HTTP logs are deliberately disabled, including ambient log settings.
    logging.getLogger("azure").setLevel(logging.CRITICAL)
    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        access_log=None,
        handler_cancellation=True,
        shutdown_timeout=6,
        print=None,
    )
