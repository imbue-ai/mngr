"""Minimal Anthropic-compatible proxy that forwards requests to the real API.

Usage:
    source .env
    uv run python litellm_proxy/proxy.py

Then in another terminal:
    ANTHROPIC_BASE_URL=http://localhost:4000 claude -p "hello"
"""

import json
import os
import sys
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.responses import StreamingResponse
from starlette.routing import Route

ANTHROPIC_API_BASE = "https://api.anthropic.com"
PORT = int(os.environ.get("LITELLM_PROXY_PORT", "4000"))

HOP_BY_HOP_HEADERS = frozenset(
    {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
    }
)


def _get_anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    return key


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


async def proxy_anthropic(request: Request) -> Response:
    """Forward any request under /v1/ to the real Anthropic API."""
    path = request.url.path
    target_url = f"{ANTHROPIC_API_BASE}{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    body = await request.body()

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    if "x-api-key" not in headers:
        headers["x-api-key"] = _get_anthropic_api_key()

    is_stream = False
    if body:
        try:
            parsed = json.loads(body)
            is_stream = parsed.get("stream", False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    async with httpx.AsyncClient() as client:
        if is_stream:
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            upstream = await client.send(req, stream=True)

            async def stream_body() -> Any:
                async for chunk in upstream.aiter_raw():
                    yield chunk
                await upstream.aclose()

            return StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                headers=_filter_response_headers(upstream.headers),
            )
        else:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
                timeout=300.0,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=_filter_response_headers(resp.headers),
            )


async def health(request: Request) -> Response:
    return Response(
        content=json.dumps({"status": "ok"}),
        media_type="application/json",
    )


app = Starlette(
    routes=[
        Route("/health", health),
        Route(
            "/v1/{path:path}", proxy_anthropic, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
        ),
    ],
)


if __name__ == "__main__":
    print(f"Starting Anthropic proxy on port {PORT}...")
    print(f"  ANTHROPIC_BASE_URL=http://localhost:{PORT} claude -p 'hello'")
    print()
    uvicorn.run(app, host="0.0.0.0", port=PORT)
