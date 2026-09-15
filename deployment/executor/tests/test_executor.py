import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from azure.core.exceptions import ClientAuthenticationError

from app import RUNTIME, create_app
from faults import SCENARIOS, synthetic_app
from executor import (
    API_VERSION,
    MAX_BODY_BYTES,
    MAX_CONCURRENCY,
    TOKEN_SCOPE,
    Attempt,
    Backend,
    BufferedResponse,
    ExecutorFailure,
    Settings,
    bounded_ms,
    buffered_post,
)

KEY = "unit-test-only-not-a-secret-0000000000"
BACKEND = Backend("https://unit-test.openai.azure.com", "chat-deployment", "chat")


class FakeCredential:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.delay = 0
        self.cancelled = False
        self.failure = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def get_token(self, scope):
        self.calls.append(scope)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failure:
            raise self.failure
        return SimpleNamespace(token="synthetic-token-never-sent-to-Azure")


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.credential = FakeCredential()
        self.upstream = TestServer(synthetic_app(), handler_cancellation=True)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)
        origin = str(self.upstream.make_url("/")).rstrip("/")
        self.app = create_app(
            Settings(KEY, {
                "chat": BACKEND,
                "embedding": Backend(BACKEND.endpoint, "embedding-deployment", "embedding"),
                **{f"test-{scenario}": Backend(origin, scenario, "chat")
                   for scenario in SCENARIOS},
            }),
            self.credential,
        )
        self.prepared = []
        self.app.on_response_prepare.append(self.record_prepare)
        self.client = TestClient(TestServer(self.app, handler_cancellation=True))
        await self.client.start_server()
        self.headers = {"X-Executor-Key": KEY}

    async def record_prepare(self, request, response):
        self.prepared.append((request.path, response.status))

    async def asyncTearDown(self):
        await self.client.close()
        self.assertTrue(self.credential.closed)
        self.assertTrue(self.app[RUNTIME].session.closed)
        self.assertEqual(self.app[RUNTIME].active, 0)

    async def post_synthetic(self, scenario, budget=1000, idle=200):
        return await self.client.post(
            f"/execute/test-{scenario}", data=b"{}",
            headers={**self.headers, "X-Budget-Ms": str(budget), "X-Idle-Ms": str(idle)},
        )

    async def assert_error(self, response, status, code):
        self.assertEqual(response.status, status)
        self.assertEqual(response.headers["x-executor-error"], code)
        self.assertEqual(await response.json(), {"error": {"code": code}})

    async def test_health_is_only_unauthenticated_endpoint(self):
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        for path in ("/execute/chat", "/fault/success", "/nonexistent"):
            for header in ({}, {"X-Executor-Key": "wrong"}, {"X-Executor-Key": "非ASCII"}):
                with self.subTest(path=path, header=header):
                    response = await self.client.post(path, headers=header)
                    await self.assert_error(response, 401, "unauthorized")
        self.assertEqual(self.credential.calls, [])

    async def test_success_waits_for_complete_body_before_headers(self):
        task = asyncio.create_task(self.post_synthetic("success"))
        try:
            await asyncio.sleep(0.025)
            self.assertFalse(task.done(), "Downstream headers must wait for the final body chunk")
            self.assertEqual(self.prepared, [])
            response = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b'{"result":"ok"}')
        self.assertEqual(self.prepared, [("/execute/test-success", 200)])
        self.assertEqual(response.headers["x-request-id"], "synthetic-request")
        self.assertIn("x-executor-attempt-id", response.headers)
        self.assertEqual(self.credential.calls, [TOKEN_SCOPE])

    async def test_actual_headers_then_body_stall_yields_only_504(self):
        response = await self.post_synthetic("body-stall", budget=700, idle=60)
        await self.assert_error(response, 504, "body_read_timeout")
        self.assertEqual(self.prepared, [("/execute/test-body-stall", 504)])

    async def test_idle_timeout_does_not_retry_and_next_request_can_succeed(self):
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await self.post_synthetic("body-stall", budget=3750, idle=700)
        await self.assert_error(response, 504, "body_read_timeout")
        elapsed_ms = (loop.time() - started) * 1000
        self.assertGreaterEqual(elapsed_ms, 650)
        self.assertLess(elapsed_ms, 1500)
        self.assertEqual(self.credential.calls, [TOKEN_SCOPE])
        self.assertEqual(self.prepared, [("/execute/test-body-stall", 504)])
        response = await self.post_synthetic("success", budget=1000, idle=700)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"result": "ok"})
        self.assertEqual(self.prepared, [
            ("/execute/test-body-stall", 504),
            ("/execute/test-success", 200),
        ])
        self.assertEqual(self.credential.calls, [TOKEN_SCOPE, TOKEN_SCOPE])

    async def test_drip_cannot_extend_total_deadline(self):
        start = asyncio.get_running_loop().time()
        response = await self.post_synthetic("drip", budget=180, idle=120)
        await self.assert_error(response, 504, "total_timeout")
        self.assertLess(asyncio.get_running_loop().time() - start, 0.6)
        self.assertEqual(self.prepared, [("/execute/test-drip", 504)])

    async def test_delayed_headers_use_total_not_body_idle_deadline(self):
        response = await self.post_synthetic("delayed-headers", budget=120, idle=20)
        await self.assert_error(response, 504, "total_timeout")

    async def test_total_wins_when_body_idle_is_longer(self):
        response = await self.post_synthetic("body-stall", budget=100, idle=500)
        await self.assert_error(response, 504, "total_timeout")

    async def test_failure_statuses_are_not_rewritten(self):
        for status in (400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                response = await self.post_synthetic(f"status{status}")
                self.assertEqual(response.status, status)
                self.assertEqual((await response.json())["error"]["code"], f"status{status}")
                self.assertEqual(response.headers["retry-after"], "2")
                self.assertEqual(response.headers["x-request-id"], "synthetic-request")
                self.assertNotIn("x-executor-error", response.headers)

    async def test_invalid_json_never_commits_success(self):
        response = await self.post_synthetic("truncated")
        await self.assert_error(response, 502, "invalid_upstream_json")
        self.assertEqual(self.prepared, [("/execute/test-truncated", 502)])

    async def test_chunked_response_limit(self):
        response = await self.post_synthetic("oversized", budget=2000)
        await self.assert_error(response, 502, "response_too_large")

    async def test_unknown_backend_and_url_routing_rejected_before_token(self):
        response = await self.client.post(
            "/execute/https:%2F%2Fattacker.invalid", headers=self.headers, json={},
        )
        await self.assert_error(response, 404, "unknown_backend")
        for key in (
            "url", "endpoint", "base_url", "api_base", "deployment", "backendId",
            "api_key", "api_key_env",
        ):
            response = await self.client.post(
                "/execute/chat", headers=self.headers,
                json={key: "https://attacker.invalid", "messages": []},
            )
            await self.assert_error(response, 400, "routing_override_forbidden")
        self.assertEqual(self.credential.calls, [])

    async def test_stream_and_model_validation(self):
        for value in (True, "false", 0, None):
            response = await self.client.post(
                "/execute/chat", headers=self.headers, json={"stream": value},
            )
            await self.assert_error(response, 400, "streaming_not_supported")
        response = await self.client.post(
            "/execute/chat", headers=self.headers, json={"model": "another-deployment"},
        )
        await self.assert_error(response, 400, "model_mismatch")
        self.assertEqual(self.credential.calls, [])

    async def test_chat_and_embedding_use_fixed_url_model_and_single_token(self):
        for name, deployment, operation in (
            ("chat", "chat-deployment", "chat/completions"),
            ("embedding", "embedding-deployment", "embeddings"),
        ):
            with self.subTest(kind=name):
                reader = AsyncMock(return_value=BufferedResponse(200, b'{"ok":true}'))
                with patch("app.buffered_post", reader):
                    response = await self.client.post(
                        f"/execute/{name}", headers={
                            **self.headers,
                            "Authorization": "Bearer must-not-forward",
                            "X-Budget-Ms": "999999",
                            "X-Idle-Ms": "999999",
                        },
                        json={"stream": False, "max_completion_tokens": 128},
                    )
                self.assertEqual(response.status, 200)
                reader.assert_awaited_once()
                args = reader.await_args.args
                self.assertEqual(args[1], (
                    f"{BACKEND.endpoint}/openai/deployments/{deployment}/"
                    f"{operation}?api-version={API_VERSION}"
                ))
                self.assertEqual(json.loads(args[2])["model"], deployment)
                self.assertEqual(json.loads(args[2])["max_completion_tokens"], 128)
                self.assertEqual(args[3], {
                    "Authorization": "Bearer synthetic-token-never-sent-to-Azure",
                })
                self.assertEqual(args[4], 5500)
        self.assertEqual(self.credential.calls, [TOKEN_SCOPE, TOKEN_SCOPE])

    async def test_credential_acquisition_is_inside_total_deadline(self):
        self.credential.delay = 10
        with patch("app.buffered_post", AsyncMock()) as reader:
            response = await self.client.post(
                "/execute/chat",
                headers={**self.headers, "X-Budget-Ms": "60"},
                json={"messages": []},
            )
        await self.assert_error(response, 504, "total_timeout")
        self.assertTrue(self.credential.cancelled)
        reader.assert_not_awaited()

    async def test_identity_error_is_sanitized(self):
        self.credential.failure = ClientAuthenticationError("secret-shaped credential failure")
        with self.assertLogs("executor", level="INFO") as captured:
            response = await self.client.post("/execute/chat", headers=self.headers, json={})
        await self.assert_error(response, 503, "identity_unavailable")
        log = json.loads(captured.records[-1].getMessage())
        self.assertEqual(log["phase"], "credential")
        self.assertEqual(log["backend"], "chat")
        self.assertEqual(log["status"], 503)
        self.assertNotIn("secret-shaped", str(log))
        self.assertNotIn(KEY, str(log))
        self.assertEqual(set(log), {"attempt_id", "backend", "status", "elapsed_ms", "phase"})

    async def test_invalid_request_json_and_oversized_input(self):
        for data, code in (
            (b"{", "invalid_request_json"),
            (b'{"value":NaN}', "invalid_request_json"),
            (b"[]", "request_must_be_object"),
        ):
            response = await self.client.post("/execute/chat", headers=self.headers, data=data)
            await self.assert_error(response, 400, code)
        response = await self.client.post(
            "/execute/chat", headers=self.headers, data=io.BytesIO(b"x" * (MAX_BODY_BYTES + 1)),
        )
        await self.assert_error(response, 413, "request_too_large")
        self.assertEqual(self.credential.calls, [])

    async def test_chunked_input_is_bounded_and_compression_is_rejected(self):
        async def oversized():
            for _ in range(MAX_BODY_BYTES // 65536 + 1):
                yield b"x" * 65536
        response = await self.client.post(
            "/execute/chat", headers=self.headers, data=oversized(),
        )
        await self.assert_error(response, 413, "request_too_large")
        response = await self.client.post(
            "/execute/chat", headers={**self.headers, "Content-Encoding": "gzip"}, data=b"{}",
        )
        await self.assert_error(response, 400, "unsupported_request_encoding")

    async def test_request_reading_is_inside_deadline(self):
        async def slow_body():
            yield b"{"
            await asyncio.sleep(1)
            yield b"}"
        response = await self.client.post(
            "/execute/chat", headers={**self.headers, "X-Budget-Ms": "80"}, data=slow_body(),
        )
        await self.assert_error(response, 504, "total_timeout")
        self.assertEqual(self.credential.calls, [])

    async def test_capacity_rejects_without_queueing_and_slots_release(self):
        tasks = [asyncio.create_task(self.post_synthetic("body-stall", budget=450, idle=350))
                 for _ in range(MAX_CONCURRENCY)]
        try:
            async with asyncio.timeout(1):
                while self.app[RUNTIME].active != MAX_CONCURRENCY:
                    await asyncio.sleep(0.005)
            start = asyncio.get_running_loop().time()
            response = await self.post_synthetic("success")
            await self.assert_error(response, 503, "capacity_exceeded")
            self.assertLess(asyncio.get_running_loop().time() - start, 0.15)
            responses = await asyncio.gather(*tasks)
            self.assertTrue(all(response.status == 504 for response in responses))
            self.assertEqual(self.app[RUNTIME].active, 0)
            response = await self.post_synthetic("success")
            self.assertEqual(response.status, 200)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_disconnect_cancels_token_work_and_releases_capacity(self):
        self.credential.delay = 10
        url = self.client.make_url("/execute/chat")
        reader, writer = await asyncio.open_connection(url.host, url.port)
        try:
            writer.write((
                f"POST /execute/chat HTTP/1.1\r\nHost: localhost\r\n"
                f"X-Executor-Key: {KEY}\r\nContent-Length: 2\r\n\r\n{{}}"
            ).encode())
            await writer.drain()
            async with asyncio.timeout(1):
                while not self.credential.calls:
                    await asyncio.sleep(0.005)
            writer.close()
            await writer.wait_closed()
            async with asyncio.timeout(1):
                while self.app[RUNTIME].active:
                    await asyncio.sleep(0.005)
            self.assertTrue(self.credential.cancelled)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_removed_demo_endpoints_are_not_registered(self):
        for path in ("/fault/success", "/fault/body-stall", "/synthetic/success"):
            response = await self.client.post(path, headers=self.headers)
            await self.assert_error(response, 404, "http_error")
        self.assertEqual(self.credential.calls, [])

    async def test_disconnect_cancels_actual_buffered_reader(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def tracked_reader(*args, **kwargs):
            entered.set()
            try:
                return await buffered_post(*args, **kwargs)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        url = self.client.make_url("/execute/test-body-stall")
        _, writer = await asyncio.open_connection(url.host, url.port)
        try:
            with patch("app.buffered_post", tracked_reader):
                writer.write((
                    f"POST /execute/test-body-stall HTTP/1.1\r\nHost: localhost\r\n"
                    f"X-Executor-Key: {KEY}\r\nContent-Length: 2\r\n\r\n{{}}"
                ).encode())
                await writer.drain()
                async with asyncio.timeout(1):
                    await entered.wait()
                await asyncio.sleep(0.03)
                writer.close()
                await writer.wait_closed()
                async with asyncio.timeout(1):
                    await cancelled.wait()
                    while self.app[RUNTIME].active:
                        await asyncio.sleep(0.005)
                self.assertEqual(self.prepared, [])
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_invalid_timeout_header(self):
        for header in ("X-Budget-Ms", "X-Idle-Ms"):
            response = await self.client.post(
                "/execute/chat", headers={**self.headers, header: "not-an-integer"},
            )
            await self.assert_error(response, 400, "invalid_timeout_header")


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None),
            auto_decompress=False,
            trust_env=False,
        )

    async def asyncTearDown(self):
        await self.session.close()
        for server in self.servers:
            await server.close()

    async def peer(self, handler):
        app = web.Application()
        app.router.add_post("/", handler)
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url("/"))

    async def read(self, url, budget=1):
        attempt = Attempt("reader-test", asyncio.get_running_loop().time() + budget)
        async with asyncio.timeout_at(attempt.deadline):
            return await buffered_post(self.session, url, b"{}", {}, 300, attempt)

    async def test_declared_size_limit_before_reading(self):
        async def respond(request):
            return web.Response(body=b" " * (MAX_BODY_BYTES + 1))
        url = await self.peer(respond)
        with self.assertRaises(ExecutorFailure) as context:
            await self.read(url)
        self.assertEqual(context.exception.code, "response_too_large")

    async def test_exact_response_limit_is_accepted(self):
        data = b'"' + b"x" * (MAX_BODY_BYTES - 2) + b'"'
        async def respond(request):
            return web.Response(body=data, content_type="application/json")
        result = await self.read(await self.peer(respond))
        self.assertEqual(result.body, data)
        self.assertEqual(result.status, 200)

    async def test_redirect_is_not_followed_and_cookies_not_forwarded(self):
        calls = []
        async def respond(request):
            calls.append(request.path)
            return web.Response(
                status=307, body=b'{"redirect":true}',
                headers={"Location": "https://must-not-contact.invalid", "Set-Cookie": "secret=no"},
            )
        result = await self.read(await self.peer(respond))
        self.assertEqual(calls, ["/"])
        self.assertEqual(result.status, 307)
        self.assertNotIn("Location", result.headers)
        self.assertNotIn("Set-Cookie", result.headers)

    async def test_connection_failure(self):
        async def respond(request):
            return web.json_response({})
        url = await self.peer(respond)
        await self.servers[-1].close()
        with self.assertRaises(ExecutorFailure) as context:
            await self.read(url)
        self.assertEqual(context.exception.status, 502)
        self.assertEqual(context.exception.code, "connection_error")

    async def test_broken_content_length_is_protocol_body_failure(self):
        async def respond(request):
            response = web.StreamResponse(headers={"Content-Length": "100"})
            await response.prepare(request)
            await response.write(b'{"partial":')
            request.transport.close()
            return response
        with self.assertRaises(ExecutorFailure) as context:
            await self.read(await self.peer(respond))
        self.assertEqual(context.exception.code, "upstream_body_error")

    async def test_invalid_error_body_is_502_not_passthrough(self):
        async def respond(request):
            return web.Response(status=503, text="<html>unavailable</html>")
        with self.assertRaises(ExecutorFailure) as context:
            await self.read(await self.peer(respond))
        self.assertEqual(context.exception.status, 502)
        self.assertEqual(context.exception.code, "invalid_upstream_json")

    async def test_total_deadline_checked_after_json_validation(self):
        async def respond(request):
            return web.json_response({"ok": True})
        url = await self.peer(respond)
        attempt = Attempt("parse-deadline", asyncio.get_running_loop().time() + 1)
        def parser(body):
            attempt.deadline = 0
            return {}
        with patch("executor.parse_json", parser):
            with self.assertRaises(TimeoutError):
                await buffered_post(self.session, url, b"{}", {}, 300, attempt)

    async def test_compressed_response_rejected_without_decompression(self):
        async def respond(request):
            return web.Response(
                body=b"not-decoded",
                headers={"Content-Encoding": "gzip"},
            )
        with self.assertRaises(ExecutorFailure) as context:
            await self.read(await self.peer(respond))
        self.assertEqual(context.exception.code, "unsupported_content_encoding")


class IdentityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_assigned_client_id_reaches_managed_identity_credential(self):
        credential = FakeCredential()
        client_id = "00000000-0000-0000-0000-000000000123"
        settings = Settings.from_env({
            "EXECUTOR_KEY": KEY,
            "AZURE_CLIENT_ID": client_id,
            "BACKENDS_JSON": json.dumps({
                "only-backend": {
                    "endpoint": BACKEND.endpoint,
                    "deployment": BACKEND.deployment,
                    "kind": BACKEND.kind,
                },
            }),
        })
        self.assertEqual(len(settings.backends), 1)
        with patch("app.ManagedIdentityCredential", return_value=credential) as factory:
            client = TestClient(TestServer(create_app(settings)))
            await client.start_server()
            try:
                response = await client.get("/health")
                self.assertEqual(response.status, 200)
                factory.assert_called_once_with(
                    client_id=client_id,
                    retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                    logging_enable=False,
                )
                self.assertEqual(credential.calls, [])
            finally:
                await client.close()
        self.assertTrue(credential.closed)


class APIKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_backend_keys_bypass_mi_and_never_use_caller_credentials(self):
        env = {
            "EXECUTOR_KEY": KEY,
            "EASTUS2_API_KEY": "unit-test-only-eastus2-key",
            "SWEDEN_API_KEY": "unit-test-only-sweden-key",
            "WESTUS3_API_KEY": "unit-test-only-westus3-key",
        }
        mappings = {
            "eastus2": {
                "endpoint": "https://svhw-openai-eastus2.openai.azure.com/",
                "deployment": "gpt-5.1",
                "kind": "chat",
                "api_key_env": "EASTUS2_API_KEY",
            },
            "sweden": {
                "endpoint": "https://svhw2-swedencentral.openai.azure.com/",
                "deployment": "svhwb107-gpt51",
                "kind": "chat",
                "api_key_env": "SWEDEN_API_KEY",
            },
            "embedding": {
                "endpoint": "https://svhw2-westus3.openai.azure.com/",
                "deployment": "text-embedding-3-small",
                "kind": "embedding",
                "api_key_env": "WESTUS3_API_KEY",
            },
            "mi": {
                "endpoint": BACKEND.endpoint,
                "deployment": BACKEND.deployment,
                "kind": BACKEND.kind,
            },
        }
        env["BACKENDS_JSON"] = json.dumps(mappings)
        settings = Settings.from_env(env)
        credential = FakeCredential()
        client = TestClient(TestServer(create_app(settings, credential)))
        await client.start_server()
        try:
            with self.assertLogs("executor", level="INFO") as logs:
                for name in ("eastus2", "sweden", "embedding"):
                    for status in (200, 401):
                        with self.subTest(backend=name, status=status):
                            reader = AsyncMock(return_value=BufferedResponse(status, b'{"ok":true}'))
                            with patch("app.buffered_post", reader):
                                response = await client.post(
                                    f"/execute/{name}",
                                    headers={
                                        "X-Executor-Key": KEY,
                                        "api-key": "caller-key-must-not-be-forwarded",
                                        "Authorization": "Bearer caller-token-must-not-be-forwarded",
                                    },
                                    json={},
                                )
                            self.assertEqual(response.status, status)
                            reader.assert_awaited_once()
                            args = reader.await_args.args
                            backend = mappings[name]
                            self.assertEqual(args[1], settings.backends[name].url)
                            self.assertEqual(json.loads(args[2])["model"], backend["deployment"])
                            self.assertEqual(args[3], {"api-key": env[backend["api_key_env"]]})
                            self.assertEqual(credential.calls, [])
                            response_text = await response.text()
                            self.assertNotIn(env[backend["api_key_env"]], response_text)
                            self.assertNotIn("api-key", response.headers)
                reader = AsyncMock(return_value=BufferedResponse(200, b"{}"))
                with patch("app.buffered_post", reader):
                    response = await client.post(
                        "/execute/mi", headers={"X-Executor-Key": KEY}, json={},
                    )
                self.assertEqual(response.status, 200)
                self.assertEqual(credential.calls, [TOKEN_SCOPE])
                self.assertIn("Authorization", reader.await_args.args[3])
                self.assertNotIn("api-key", reader.await_args.args[3])
            for name in ("EXECUTOR_KEY", "EASTUS2_API_KEY", "SWEDEN_API_KEY", "WESTUS3_API_KEY"):
                self.assertNotIn(env[name], repr(settings))
                self.assertNotIn(env[name], "\n".join(logs.output))
        finally:
            await client.close()
        self.assertTrue(credential.closed)


class ConfigTests(unittest.TestCase):
    def settings(self, endpoint=BACKEND.endpoint, **updates):
        env = {
            "EXECUTOR_KEY": KEY,
            "BACKENDS_JSON": json.dumps({
                "chat": {"endpoint": endpoint, "deployment": "chat-deployment", "kind": "chat"},
            }),
        }
        env.update(updates)
        return Settings.from_env(env)

    def test_valid_config(self):
        self.assertEqual(self.settings().backends["chat"], BACKEND)
        self.assertIsNone(self.settings().identity_client_id)

    def test_untrusted_endpoint_config_rejected(self):
        for endpoint in (
            "http://unit-test.openai.azure.com",
            "https://openai.azure.com.attacker.invalid",
            "https://attacker.invalid",
            "https://127.0.0.1",
            "https://user:pass@unit-test.openai.azure.com",
            BACKEND.endpoint + "/path",
            BACKEND.endpoint + "?url=https://attacker.invalid",
            BACKEND.endpoint + "#fragment",
            BACKEND.endpoint + ":8443",
            BACKEND.endpoint + "\n",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    self.settings(endpoint)

    def test_invalid_config(self):
        for updates in (
            {"EXECUTOR_KEY": ""},
            {"BACKENDS_JSON": "["},
            {"BACKENDS_JSON": "[]"},
            {"BACKENDS_JSON": "{}"},
            {"BACKENDS_JSON": '{"chat":{"endpoint":"https://x.openai.azure.com","deployment":"../x","kind":"chat"}}'},
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.settings(**updates)

    def test_timeout_clamps(self):
        self.assertEqual(bounded_ms({}, "budget", 5500, 5500), 5500)
        self.assertEqual(bounded_ms({"budget": "999999"}, "budget", 5500, 5500), 5500)
        self.assertEqual(bounded_ms({"budget": "-10"}, "budget", 5500, 5500), 1)
        self.assertEqual(bounded_ms({"budget": "0"}, "budget", 5500, 5500), 1)

    def test_api_key_env_missing_empty_or_invalid_fails_startup(self):
        mapping = {
            "chat": {
                "endpoint": BACKEND.endpoint,
                "deployment": BACKEND.deployment,
                "kind": BACKEND.kind,
                "api_key_env": "CHAT_API_KEY",
            },
        }
        for value in (None, "", "contains whitespace", "contains\r\nnewline", "非ASCII", "x" * 4097):
            with self.subTest(value=value):
                env = {"EXECUTOR_KEY": KEY, "BACKENDS_JSON": json.dumps(mapping)}
                if value is not None:
                    env["CHAT_API_KEY"] = value
                with patch("app.ManagedIdentityCredential") as factory:
                    with self.assertRaisesRegex(ValueError, "CHAT_API_KEY is missing or invalid") as error:
                        create_app(Settings.from_env(env))
                    factory.assert_not_called()
                if value:
                    self.assertNotIn(value, str(error.exception))

    def test_invalid_api_key_reference_is_not_silent_mi_fallback(self):
        for name in (None, "", 123, ["CHAT_API_KEY"], "api-key", "prefix\nkey"):
            with self.subTest(name=name):
                mapping = {
                    "chat": {
                        "endpoint": BACKEND.endpoint,
                        "deployment": BACKEND.deployment,
                        "kind": BACKEND.kind,
                        "api_key_env": name,
                    },
                }
                with self.assertRaisesRegex(ValueError, "Invalid API key environment variable name"):
                    self.settings(BACKENDS_JSON=json.dumps(mapping))


if __name__ == "__main__":
    unittest.main()
