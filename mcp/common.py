"""Shared MCP scaffolding: transport, auth, bounds, backend calls.

THE TWO RULES EVERY SERVER FOLLOWS

1. No passthrough tools. No tool takes a raw PromQL, LogQL, GraphQL or CLI
   string. Every query is composed server-side from validated arguments. One
   passthrough tool makes every other boundary in the stack decorative, and it
   is very hard to remove once something depends on it.

2. Every argument is bounded before use. Device names against a regex, time
   windows and result limits capped here rather than trusted from the caller.

WHO MAY CALL — MCP_AUTH_MODE

  token   the shared MCP_AUTH_TOKEN (default). One secret, no identity.
  oidc    the caller's own JWT from the ai-platform's identity provider,
          checked against its JWKS. Every call is attributable to a person,
          and roles in the token gate the write tool.
  both    either. For cut-over, and for tooling on the VM that has the token.
"""
from __future__ import annotations

import functools
import hmac
import inspect
import json
import logging
import os
import re
from typing import Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
AUTH_MODE = (os.environ.get("MCP_AUTH_MODE") or "token").strip().lower()
OIDC_ISSUER = os.environ.get("MCP_OIDC_ISSUER", "").strip()
OIDC_JWKS_URL = os.environ.get("MCP_OIDC_JWKS_URL", "").strip()
OIDC_AUDIENCES = [a.strip() for a in os.environ.get("MCP_OIDC_AUDIENCES", "").split(",") if a.strip()]
WRITE_ROLE = os.environ.get("MCP_WRITE_ROLE", "").strip() or "darqcube-write"

# 512 KiB suits a large model. A small local model's whole context can be
# smaller than that, so a deployment can lower it (site.yml max_response_kb).
MAX_RESPONSE_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES") or 512 * 1024)

AUTH_MODES = ("token", "oidc", "both")

log = logging.getLogger("mcp.audit")

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


def literal(value: str | None, what: str = "term", maximum: int = 256) -> str | None:
    """A free-text search term, length-capped. It is escaped where it is used;
    this only stops a caller sending a megabyte of it."""
    if value is not None and len(value) > maximum:
        raise BoundsError(f"{what} is {len(value)} characters, over the {maximum} limit")
    return value


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
    characters of {"name": {"value": "router1"}} noise. A model reading that
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


# --- who is calling --------------------------------------------------------

class AuthError(Exception):
    """The request carries no acceptable credential."""


class Verifier:
    """Decides who a bearer credential belongs to, per MCP_AUTH_MODE.

    Returns a caller dict: {"user", "sub", "roles", "via"}. A token caller is
    the operator: it holds the one secret the stack was installed with, so it
    is trusted the way the shared token always has been.
    """

    def __init__(self, mode: str = AUTH_MODE, token: str = AUTH_TOKEN, issuer: str = OIDC_ISSUER,
                 jwks_url: str = OIDC_JWKS_URL, audiences: list[str] | None = None):
        if mode not in AUTH_MODES:
            raise SystemExit(f"MCP_AUTH_MODE must be one of {', '.join(AUTH_MODES)} (got {mode!r})")
        # Fail at startup, not at the first request: an empty token used to
        # mean "no auth at all", silently.
        if mode in ("token", "both") and not token:
            raise SystemExit(f"MCP_AUTH_MODE={mode} needs MCP_AUTH_TOKEN")
        if mode in ("oidc", "both") and not (issuer and jwks_url):
            raise SystemExit(f"MCP_AUTH_MODE={mode} needs MCP_OIDC_ISSUER and MCP_OIDC_JWKS_URL")
        self.mode, self.token, self.issuer = mode, token, issuer
        self.audiences = audiences if audiences is not None else OIDC_AUDIENCES
        self._jwks = None
        if mode in ("oidc", "both"):
            import jwt  # pyjwt[crypto] — only needed when OIDC is on

            self._jwt = jwt
            # Keys are cached, and refetched when a token names an unknown kid,
            # so an identity-provider key rotation needs no restart.
            self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=10)

    def verify(self, header: str) -> dict:
        if not header.lower().startswith("bearer "):
            raise AuthError("missing bearer credential")
        credential = header[7:].strip()

        if self.mode in ("token", "both") and hmac.compare_digest(credential, self.token):
            return {"user": "token", "sub": "token", "roles": ["*"], "via": "token"}
        if self.mode == "token":
            raise AuthError("bad token")
        return self._verify_jwt(credential)

    def _verify_jwt(self, credential: str) -> dict:
        jwt = self._jwt
        try:
            key = self._jwks.get_signing_key_from_jwt(credential).key
            claims = jwt.decode(
                credential, key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"],
                issuer=self.issuer,
                audience=self.audiences or None,
                options={"require": ["exp", "iss", "sub"], "verify_aud": bool(self.audiences)},
                leeway=30,
            )
        except jwt.PyJWKClientError as exc:
            raise AuthError(f"cannot fetch signing keys: {exc}") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}") from exc

        roles = set((claims.get("realm_access") or {}).get("roles") or [])
        for client in (claims.get("resource_access") or {}).values():
            roles |= set((client or {}).get("roles") or [])
        roles |= set(claims.get("roles") or [])
        return {
            "user": claims.get("preferred_username") or claims.get("email") or claims["sub"],
            "sub": claims["sub"],
            "roles": sorted(roles),
            "via": "oidc",
        }


class _Auth:
    """Bearer auth plus one audit line per MCP request.

    Plain ASGI rather than BaseHTTPMiddleware: it must not buffer the
    streaming responses streamable-HTTP sends. /healthz is exempt so
    orchestration can probe it.

    The caller is stored on the request scope, where a tool reads it through
    its MCP Context (caller()). A ContextVar would not reach the tool: in a
    stateful session the tool runs in the session's task, not the request's.
    """

    def __init__(self, app, verifier: Verifier, server: str):
        self.app, self.verifier, self.server = app, verifier, server

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            caller = self.verifier.verify(headers.get("authorization", ""))
        except AuthError as exc:
            log.warning("audit denied server=%s reason=%s", self.server, exc)
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            return await response(scope, receive, send)

        scope.setdefault("state", {})["mcp_caller"] = caller

        body = bytearray()

        async def recording_receive():
            message = await receive()
            if message.get("type") == "http.request" and len(body) < 65536:
                body.extend(message.get("body", b""))
            return message

        await self.app(scope, recording_receive, send)
        # Prefixed so `grep audit` finds it whatever log format is active —
        # the MCP SDK configures a bare %(message)s format.
        log.info("audit user=%s sub=%s via=%s server=%s %s", caller["user"], caller["sub"],
                 caller["via"], self.server, _rpc_summary(bytes(body)))


def _rpc_summary(body: bytes) -> str:
    """`rpc=tools/call tool=get_device_state` — what was asked, never the arguments."""
    try:
        message = json.loads(body or b"{}")
    except ValueError:
        return "rpc=?"
    messages = message if isinstance(message, list) else [message]
    parts = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        part = f"rpc={m.get('method', 'response')}"
        tool = (m.get("params") or {}).get("name") if m.get("method") == "tools/call" else None
        parts.append(part + (f" tool={tool}" if tool else ""))
    return " ".join(parts) or "rpc=-"


def caller(ctx: Context) -> dict:
    """The authenticated caller of the current tool call."""
    request = getattr(ctx.request_context, "request", None)
    found = getattr(getattr(request, "state", None), "mcp_caller", None)
    if not found:
        raise PermissionError("no authenticated caller on this request")
    return found


def require_role(ctx: Context, role: str = WRITE_ROLE) -> dict:
    """Refuse the call unless the caller holds `role`.

    A token caller passes: the shared token is the operator's own credential.
    An OIDC caller must carry the role in their token — which is what lets the
    same tool be allowed for one person and refused for another.
    """
    who = caller(ctx)
    if "*" in who["roles"] or role in who["roles"]:
        return who
    raise PermissionError(f"{who['user']} lacks the '{role}' role this tool requires")


# --- the server ------------------------------------------------------------

def _in_thread(fn):
    """Run a blocking tool in a worker thread.

    FastMCP calls a sync tool directly on the event loop, so one slow device
    call (a config fetch can take minutes) would stall every other request to
    this server — including health checks and other agents' calls.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


class _Server(FastMCP):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            return register(fn if inspect.iscoroutinefunction(fn) else _in_thread(fn))
        return decorator


def allowed_hosts(name: str) -> list[str]:
    """Host headers this server answers to.

    The MCP SDK refuses any other Host with 421, as DNS-rebinding protection.
    Always: loopback and this server's own compose name. Plus MCP_ALLOWED_HOSTS
    — the address an ai-platform on another machine uses (install.py derives
    it from ai_platform.publish.bind_ip). Patterns are `host:port` or `host:*`.
    """
    hosts = ["localhost:*", "127.0.0.1:*", "[::1]:*", f"mcp-{name}:*"]
    for item in os.environ.get("MCP_ALLOWED_HOSTS", "").split(","):
        item = item.strip()
        if item and item not in hosts:
            hosts.append(item)
    return hosts


def build(name: str) -> FastMCP:
    # host= is set because FastMCP's default of 127.0.0.1 silently turns on a
    # loopback-only Host allow-list, and every call through the compose
    # network then fails with 421 while /healthz stays green. The explicit
    # allow-list below replaces it. (uvicorn's bind address is in serve().)
    return _Server(
        name,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(name),
            allowed_origins=[],
        ),
    )


def serve(mcp: FastMCP, port: int) -> None:
    """Run over streamable-HTTP with auth and a health endpoint."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
    verifier = Verifier()
    app = mcp.streamable_http_app()
    app.router.routes.append(
        Route("/healthz", lambda r: PlainTextResponse("ok"), methods=["GET"])
    )
    import uvicorn

    print(f"auth mode: {verifier.mode}", flush=True)
    uvicorn.run(_Auth(app, verifier, mcp.name), host="0.0.0.0", port=port, log_level="info")
