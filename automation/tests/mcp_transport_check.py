"""Runs INSIDE darqcube/mcp:local, driven by test_mcp_transport.py.

Exercises the real ASGI app — FastMCP's streamable-HTTP transport behind the
stack's auth middleware — with Starlette's TestClient. No network, no backend:
the automation API address points at a closed port, so a tool that gets past
its gates fails with "unreachable", which is itself the proof it got past them.

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

# Before anything imports common: it reads its settings at import time.
os.environ.update({
    "MCP_ALLOW_WRITE": "true",
    "MCP_ALLOWED_HOSTS": "192.0.2.10:*",
    "AUTOMATION_URL": "http://127.0.0.1:9",
    "MCP_AUTH_TOKEN": "test-token",
})
sys.path.insert(0, "/app")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from servers import netmiko  # noqa: E402

ISSUER = "http://idp.example/realms/demo"
AUDIENCE = "ai-platform"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
# The verifier's key client fetches nothing: it is handed the JWKS directly.
jwt.PyJWKClient.fetch_data = lambda self: {"keys": [JWK]}

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "check", "version": "0"}}}
results: dict[str, list] = {}


def token(roles=(), **overrides) -> str:
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u-1", "preferred_username": "alice",
              "exp": int(time.time()) + 300, "realm_access": {"roles": list(roles)}}
    claims.update(overrides)
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})


def verifier(mode: str) -> common.Verifier:
    return common.Verifier(mode=mode, token="test-token", issuer=ISSUER,
                           jwks_url="http://idp.example/jwks", audiences=[AUDIENCE])


# One app for every mode: FastMCP's session manager can be started only once
# per instance, so the client stays open and only the verifier is swapped.
APP = common._Auth(netmiko.mcp.streamable_http_app(), verifier("token"), "netmiko")


def messages(response) -> list[dict]:
    """JSON-RPC messages from a JSON or an SSE response body."""
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def initialize(client, auth: str, host: str = "127.0.0.1:9005"):
    return client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": auth, "Host": host})


def call_push(client, auth: str) -> str:
    """Open a session and call push_device_config; return the tool's text."""
    first = initialize(client, auth)
    session = {"mcp-session-id": first.headers["mcp-session-id"], "Authorization": auth,
               "Host": "127.0.0.1:9005", **HEADERS}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
    reply = client.post("/mcp", headers=session, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "push_device_config", "arguments": {"device": "router1", "lines": ["x"]}}})
    return json.dumps(messages(reply))


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


# The DNS-rebinding allow-list ------------------------------------------------
# The SDK checks Host BEFORE the session manager runs, so these need no session.

def host_checks():
    APP.verifier = verifier("token")
    auth = "Bearer test-token"
    check("host_compose_name_accepted",
          lambda: (initialize(c, auth, "mcp-netmiko:9005").status_code == 200,
                   initialize(c, auth, "mcp-netmiko:9005").status_code))
    check("host_published_address_accepted",
          lambda: (initialize(c, auth, "192.0.2.10:9005").status_code == 200,
                   initialize(c, auth, "192.0.2.10:9005").status_code))
    check("host_loopback_accepted",
          lambda: (initialize(c, auth, "localhost:9005").status_code == 200, "localhost"))
    check("host_unknown_refused",
          lambda: (initialize(c, auth, "evil.example:9005").status_code == 421,
                   initialize(c, auth, "evil.example:9005").status_code))


def token_checks():
    APP.verifier = verifier("token")
    check("token_missing_401", lambda: (c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401, ""))
    check("token_wrong_401", lambda: (initialize(c, "Bearer nope").status_code == 401, ""))
    check("token_right_200", lambda: (initialize(c, "Bearer test-token").status_code == 200, ""))
    check("token_jwt_refused_in_token_mode",
          lambda: (initialize(c, f"Bearer {token(['darqcube-write'])}").status_code == 401, ""))
    check("healthz_needs_no_auth", lambda: (c.get("/healthz").status_code in (200, 404), ""))
    # Token caller = the operator: passes the role gate, then fails reaching the API.
    out = call_push(c, "Bearer test-token")
    check("token_caller_passes_role_gate", lambda: ("unreachable" in out and "lacks" not in out, out))


def oidc_checks():
    APP.verifier = verifier("oidc")
    check("oidc_valid_200", lambda: (initialize(c, f"Bearer {token()}").status_code == 200, ""))
    check("oidc_shared_token_refused",
          lambda: (initialize(c, "Bearer test-token").status_code == 401, ""))
    check("oidc_wrong_issuer_401",
          lambda: (initialize(c, f"Bearer {token(iss='http://other/realms/x')}").status_code == 401, ""))
    check("oidc_wrong_audience_401",
          lambda: (initialize(c, f"Bearer {token(aud='someone-else')}").status_code == 401, ""))
    check("oidc_expired_401",
          lambda: (initialize(c, f"Bearer {token(exp=int(time.time()) - 600)}").status_code == 401, ""))
    viewer = call_push(c, f"Bearer {token()}")
    check("oidc_without_role_is_refused_by_the_tool",
          lambda: ("lacks the 'darqcube-write' role" in viewer, viewer))
    engineer = call_push(c, f"Bearer {token(['darqcube-write'])}")
    check("oidc_with_role_passes_the_gate",
          lambda: ("unreachable" in engineer and "lacks" not in engineer, engineer))


def both_checks():
    APP.verifier = verifier("both")
    check("both_accepts_token", lambda: (initialize(c, "Bearer test-token").status_code == 200, ""))
    check("both_accepts_jwt", lambda: (initialize(c, f"Bearer {token()}").status_code == 200, ""))
    check("both_refuses_garbage", lambda: (initialize(c, "Bearer x.y.z").status_code == 401, ""))


def startup_checks():
    def refuses(**kw):
        try:
            common.Verifier(**kw)
        except SystemExit:
            return True
        return False
    check("empty_token_refused_at_startup", lambda: (refuses(mode="token", token=""), ""))
    check("oidc_without_issuer_refused_at_startup",
          lambda: (refuses(mode="oidc", token="", issuer="", jwks_url=""), ""))
    check("unknown_mode_refused_at_startup", lambda: (refuses(mode="open", token="x"), ""))


def offload_checks():
    tools = netmiko.mcp._tool_manager.list_tools()
    check("tools_run_off_the_event_loop",
          lambda: (all(t.is_async for t in tools), {t.name: t.is_async for t in tools}))
    push = next(t for t in tools if t.name == "push_device_config")
    check("context_is_not_exposed_as_an_argument",
          lambda: ("ctx" not in push.parameters.get("properties", {}), push.parameters))


with TestClient(APP) as c:
    for group in (host_checks, token_checks, oidc_checks, both_checks, startup_checks, offload_checks):
        try:
            group()
        except Exception:
            results[group.__name__] = [False, traceback.format_exc()[-600:]]

print(json.dumps(results))
