"""Shared MCP scaffolding: transport, auth, bounds, backend calls.

THE TWO RULES EVERY SERVER FOLLOWS

1. No passthrough tools. No tool takes a raw PromQL, LogQL, GraphQL or CLI
   string. Every query is composed server-side from validated arguments. One
   passthrough tool makes every other boundary in the stack decorative, and it
   is very hard to remove once something depends on it.

2. Every argument is bounded before use. Device names against a regex, time
   windows and result limits capped here rather than trusted from the caller.
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
MAX_RESPONSE_BYTES = 512 * 1024

# Device and label values that reach a backend query. Deliberately strict:
# these arrive from an AI platform, not from a person.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DURATION = re.compile(r"^[0-9]{1,4}[smhd]$")


class BoundsError(ValueError):
    """An argument failed validation before it reached a backend."""


def identifier(value: str, what: str = "name") -> str:
    if not value or not _IDENTIFIER.fullmatch(value):
        raise BoundsError(f"invalid {what}: {value!r}")
    return value


def duration(value: str, default: str = "1h", max_hours: int = 24) -> str:
    """A time window, capped. An unbounded range is how a tool call turns into
    a multi-gigabyte backend query."""
    value = value or default
    if not _DURATION.fullmatch(value):
        raise BoundsError(f"invalid duration: {value!r} (use e.g. 15m, 6h, 2d)")
    n, unit = int(value[:-1]), value[-1]
    hours = {"s": n / 3600, "m": n / 60, "h": n, "d": n * 24}[unit]
    if hours > max_hours:
        raise BoundsError(f"duration {value} exceeds the {max_hours}h limit")
    return value


def limit(value: int | None, default: int = 100, maximum: int = 1000) -> int:
    if value is None:
        return default
    return max(1, min(int(value), maximum))


class Backend:
    """HTTP client for one backing service, with a hard response cap."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers=headers or {})

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: dict | None = None) -> Any:
        return self._request("POST", path, json=json)

    def _request(self, method: str, path: str, **kw) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.request(method, url, **kw)
        except httpx.RequestError as exc:
            raise RuntimeError(f"{self.base_url} unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")

        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError(
                f"{path} returned {len(body)} bytes, over the "
                f"{MAX_RESPONSE_BYTES} limit — narrow the query"
            )
        try:
            return response.json()
        except ValueError:
            return response.text


def flatten(node: Any) -> Any:
    """Strip Infrahub's {"value": x} envelope, recursively.

    Infrahub wraps every attribute, so an 11-device query becomes thousands of
    characters of {"name": {"value": "cr1"}} noise. A model reading that
    miscounts and misquotes; flattened, it reads what is actually there.
    """
    if isinstance(node, dict):
        if set(node) == {"value"}:
            return node["value"]
        if "value" in node and len(node) <= 2 and "__typename" in node:
            return node["value"]
        return {k: flatten(v) for k, v in node.items() if k != "__typename"}
    if isinstance(node, list):
        return [flatten(v) for v in node]
    return node


class _Auth(BaseHTTPMiddleware):
    """Shared bearer token. /healthz is exempt so orchestration can probe it."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz" or not AUTH_TOKEN:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.removeprefix("Bearer ").strip() != AUTH_TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build(name: str) -> FastMCP:
    return FastMCP(name)


def serve(mcp: FastMCP, port: int) -> None:
    """Run over streamable-HTTP with auth and a health endpoint."""
    app = mcp.streamable_http_app()
    app.router.routes.append(
        Route("/healthz", lambda r: PlainTextResponse("ok"), methods=["GET"])
    )
    app.add_middleware(_Auth)
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
