"""Test-only synthetic Azure OpenAI peer, started independently by tests."""

import asyncio

from aiohttp import web

from executor import MAX_BODY_BYTES

SCENARIOS = frozenset({
    "success", "body-stall", "drip", "delayed-headers", "truncated",
    "oversized", "status400", "status401", "status403", "status429",
    "status500", "status503",
})


def synthetic_app() -> web.Application:
    async def respond(request: web.Request):
        scenario = request.match_info["scenario"]
        if scenario not in SCENARIOS:
            return web.Response(status=404)
        await request.read()
        if scenario == "delayed-headers":
            await asyncio.sleep(10)
        if scenario.startswith("status"):
            return web.json_response(
                {"error": {"code": scenario, "message": "Synthetic upstream failure"}},
                status=int(scenario[6:]),
                headers={"retry-after": "2", "x-request-id": "synthetic-request"},
            )
        response = web.StreamResponse(headers={
            "Content-Type": "application/json",
            "x-request-id": "synthetic-request",
        })
        await response.prepare(request)
        try:
            if scenario == "body-stall":
                await response.write(b'{"result":')
                await asyncio.sleep(10)
                await response.write(b'"late"}')
            elif scenario == "drip":
                await response.write(b'{"result":"')
                for _ in range(400):
                    await response.write(b"x")
                    await asyncio.sleep(0.025)
                await response.write(b'"}')
            elif scenario == "truncated":
                await response.write(b'{"result":')
            elif scenario == "oversized":
                await response.write(b'{"result":"')
                for _ in range(MAX_BODY_BYTES // 65536 + 1):
                    await response.write(b"x" * 65536)
                await response.write(b'"}')
            else:
                await response.write(b'{"result":')
                await asyncio.sleep(0.075)
                await response.write(b'"ok"}')
            await response.write_eof()
        except ConnectionResetError:
            # Expected when the executor deadline closes this connection.
            pass
        return response

    app = web.Application(client_max_size=MAX_BODY_BYTES, handler_args={
        "handler_cancellation": True,
        "auto_decompress": False,
        "lingering_time": 0,
    })
    app.router.add_post("/openai/deployments/{scenario}/chat/completions", respond)
    return app
