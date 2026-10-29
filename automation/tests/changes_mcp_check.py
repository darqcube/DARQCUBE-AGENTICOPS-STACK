"""Runs INSIDE darqcube/mcp:local, driven by test_changes_mcp.py.

The approved-changes gates of mcp-netmiko, on the real ASGI app: the MCP
tools behind the stack's auth middleware, and the /changes routes for people.
The automation API is replaced by a recorder (the rules themselves are tested
offline in test_changes.py), so what is pinned here is who gets through which
door and what identity reaches the rules:

- an agent's token (MCP audience) can stage and commit, never approve
- the approvals audience is the only key to /changes, and it opens no tool
- the verified person, their roles, the agent (azp) and the sign-in time are
  what reach the rules, never values a caller sent
- a refusal reaches the model as a refusal with its reason
- a person without the approve right sees only their own changes, no audit

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback

os.environ.update({
    "MCP_ALLOW_WRITE": "true",
    "WRITE_MODE": "approved",
    "MCP_AUTH_TOKEN": "test-token",
    "MCP_AUTH_MODE": "both",
    "MCP_OIDC_ISSUER": "http://idp.example/realms/demo",
    "MCP_OIDC_JWKS_URL": "http://idp.example/jwks",
    "MCP_OIDC_AUDIENCES": "ai-platform",
    "AUTOMATION_URL": "http://127.0.0.1:9",
})
sys.path.insert(0, "/app")

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from servers import netmiko  # noqa: E402

ISSUER = os.environ["MCP_OIDC_ISSUER"]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
jwt.PyJWKClient.fetch_data = lambda self: {"keys": [JWK]}

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "check", "version": "0"}}}
results: dict[str, list] = {}


def token(aud="ai-platform", roles=("darqcube-propose", "darqcube-commit"), sub="u-1", user="alice",
          azp="agent-change-executor", auth_time=None) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": aud, "sub": sub, "preferred_username": user, "azp": azp,
              "iat": now, "exp": now + 300, "realm_access": {"roles": list(roles)}}
    if auth_time is not None:
        claims["auth_time"] = auth_time
    return "Bearer " + jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})


class Recorder:
    """Stands in for the automation API: records each call, answers like it."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.refuse: str | None = None

    def _change(self, actor, state="waiting", sub=None):
        return {"id": "7f3a-c21e", "device": "router1", "state": state, "lines": ["x"], "rollback": [],
                "fingerprint": "f" * 64, "requester": actor.get("user") if actor else "bob",
                "requester_sub": sub or (actor.get("sub") if actor else "u-2"), "staged_at": time.time(),
                "approve_by": time.time() + 7200}

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if self.refuse:
            raise RuntimeError(f"POST {path} -> 403: " + __import__("json").dumps({"detail": self.refuse}))
        return self._change((json or {}).get("actor"), "approved" if path.endswith("/approve") else "waiting")

    def get(self, path, params=None):
        self.calls.append(("GET", path, None))
        if path.startswith("/changes/events"):
            return {"events": [{"action": "stage", "outcome": "ok"}]}
        if path.startswith("/changes?"):
            return {"changes": [self._change(None, sub="u-1"), {**self._change(None, sub="u-2"), "id": "aaaa-bbbb"}]}
        return self._change(None, sub="u-2")


API = Recorder()
netmiko.api = API

app = netmiko.mcp.streamable_http_app()
app.router.routes.extend(netmiko.PUBLIC_ROUTES)
prefixes = tuple(r.path.split("{", 1)[0] for r in netmiko.PUBLIC_ROUTES)
APP = common._Auth(app, common.Verifier(), "netmiko", prefixes)


def messages(response) -> list[dict]:
    if response.headers.get("content-type", "").startswith("application/json"):
        return [response.json()]
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


def session(client, auth: str) -> dict:
    first = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": auth, "Host": "127.0.0.1:9005"})
    if first.status_code != 200:
        return {"status": first.status_code}
    s = {"mcp-session-id": first.headers["mcp-session-id"], "Authorization": auth, "Host": "127.0.0.1:9005", **HEADERS}
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=s)
    return s


def rpc(client, s: dict, method: str, params: dict) -> dict:
    reply = client.post("/mcp", headers=s, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
    return messages(reply)[-1]


def call(client, s, name, arguments) -> dict:
    return rpc(client, s, "tools/call", {"name": name, "arguments": arguments})["result"]


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


capture = Capture()
logging.getLogger("mcp.audit").addHandler(capture)
logging.getLogger("mcp.audit").setLevel(logging.INFO)

with TestClient(APP) as c:
    agent = session(c, token())

    def tools_list():
        names = sorted(t["name"] for t in rpc(c, agent, "tools/list", {})["result"]["tools"])
        want = ["commit_device_change", "get_device_change", "get_device_config", "get_device_state",
                "rollback_device_change", "stage_device_change", "withdraw_device_change"]
        return names == want and not [n for n in names if "approve" in n or "push" in n], names
    check("tools_are_stage_commit_and_never_approve", tools_list)

    def stage_identity():
        API.calls.clear()
        result = call(c, agent, "stage_device_change", {"device": "router1", "lines": ["interface Ethernet0/1"],
                                                        "source": "chat"})
        method, path, body = API.calls[-1]
        actor = body["actor"]
        ok = (path == "/changes" and actor["sub"] == "u-1" and actor["user"] == "alice"
              and actor["agent"] == "agent-change-executor" and actor["via"] == "oidc"
              and "darqcube-propose" in actor["roles"] and not result.get("isError"))
        return ok, actor
    check("stage_passes_the_verified_person_and_agent", stage_identity)

    def refusal_reaches_the_model():
        API.refuse = "change 7f3a-c21e is waiting, not approved"
        try:
            result = call(c, agent, "commit_device_change", {"change_id": "7f3a-c21e"})
        finally:
            API.refuse = None
        text = json.dumps(result)
        return result.get("isError") and "refused: change 7f3a-c21e is waiting, not approved" in text, text[:300]
    check("a_refusal_reaches_the_model_with_its_reason", refusal_reaches_the_model)

    def bad_id():
        before = len(API.calls)
        result = call(c, agent, "commit_device_change", {"change_id": "../../device/router1/config"})
        return result.get("isError") and len(API.calls) == before, json.dumps(result)[:200]
    check("a_malformed_change_id_never_reaches_the_api", bad_id)

    def mcp_refuses_approvals_token():
        s = session(c, token(aud="darqcube-approvals", roles=("darqcube-approve",), azp="approvals-app"))
        return s.get("status") == 401, s.get("status")
    check("mcp_refuses_an_approvals_token", mcp_refuses_approvals_token)

    fresh = int(time.time()) - 5
    approver = token(aud="darqcube-approvals", roles=("darqcube-approve", "darqcube-commit"), sub="u-9",
                     user="manager", azp="approvals-app", auth_time=fresh)

    def route_refuses_agent_token():
        r = c.get("/changes", headers={"Authorization": token()})
        return r.status_code == 401, r.status_code
    check("approvals_route_refuses_an_agents_token", route_refuses_agent_token)

    def route_refuses_shared_token():
        r = c.get("/changes", headers={"Authorization": "Bearer test-token"})
        return r.status_code == 401, r.status_code
    check("approvals_route_refuses_the_shared_token", route_refuses_shared_token)

    def approve_passes_sign_in_time():
        API.calls.clear()
        r = c.post("/changes/7f3a-c21e/approve", headers={"Authorization": approver}, json={"reason": "ok"})
        method, path, body = API.calls[-1]
        actor = body["actor"]
        ok = (r.status_code == 200 and path == "/changes/7f3a-c21e/approve" and actor["sub"] == "u-9"
              and actor["auth_time"] == fresh and actor["agent"] == "approvals-app" and body["reason"] == "ok")
        return ok, (r.status_code, actor)
    check("approve_passes_the_person_and_their_sign_in_time", approve_passes_sign_in_time)

    def approve_refusal_is_403():
        API.refuse = "an approval needs a fresh sign-in (within 300 s)"
        try:
            r = c.post("/changes/7f3a-c21e/approve", headers={"Authorization": approver})
        finally:
            API.refuse = None
        return r.status_code == 403 and "fresh sign-in" in r.text, (r.status_code, r.text[:200])
    check("an_approval_refusal_is_a_403_with_its_reason", approve_refusal_is_403)

    def approver_sees_all():
        r = c.get("/changes", headers={"Authorization": approver})
        return r.status_code == 200 and len(r.json()["changes"]) == 2 and r.json()["can_approve"], r.text[:200]
    check("an_approver_sees_every_change", approver_sees_all)

    asker = token(aud="darqcube-approvals", roles=("darqcube-propose",), sub="u-1", user="alice", azp="approvals-app")

    def asker_sees_own():
        r = c.get("/changes", headers={"Authorization": asker})
        ids = [x["id"] for x in r.json()["changes"]]
        return r.status_code == 200 and ids == ["7f3a-c21e"] and not r.json()["can_approve"], ids
    check("a_person_without_the_approve_right_sees_only_their_changes", asker_sees_own)

    def asker_no_audit():
        r = c.get("/changes/events", headers={"Authorization": asker})
        return r.status_code == 403, r.status_code
    check("the_audit_is_for_approvers", asker_no_audit)

    def others_change_hidden():
        r = c.get("/changes/7f3a-c21e", headers={"Authorization": asker})  # the recorder's change is u-2's
        return r.status_code == 404, r.status_code
    check("someone_elses_change_is_not_found", others_change_hidden)

    def bad_route_id():
        r = c.post("/changes/not-an-id/approve", headers={"Authorization": approver})
        return r.status_code == 400, r.status_code
    check("a_malformed_change_id_is_400", bad_route_id)

    def audit_names_agent():
        line = next((m for m in capture.records if "tool=stage_device_change" in m), "")
        return "agent=agent-change-executor" in line and "user=alice" in line, line
    check("the_audit_line_names_the_agent", audit_names_agent)

print(json.dumps(results))
