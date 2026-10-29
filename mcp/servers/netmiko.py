"""mcp-netmiko — what the device itself says, and (optionally) changing it.

This server does NOT open SSH sessions. It calls the automation API, which owns
the credentials, the TextFSM parsing and the error handling. One code path to
devices means a capability added there is available here for free, and a fix
never has to be made twice.

CHANGING A DEVICE (MCP_ALLOW_WRITE=true), per WRITE_MODE:

  approved  (default) an agent STAGES a change, a person with the approve right
            approves it through /changes below — a path an agent's token cannot
            use — and the agent COMMITS it once, within its window. The rules
            live in automation/changes/; docs/how-to/approve-changes.md.
  direct    push_device_config, one step, gated by MCP_WRITE_ROLE only.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import anyio
from mcp.server.fastmcp import Context
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from common import (OIDC_AUDIENCES, OIDC_ISSUER, OIDC_JWKS_URL, AuthError, Backend, Verifier, build, caller,
                    identifier, log, require_role)

# 300s: a config push is four sequential device commands — fetch the running
# config, snapshot state, push, snapshot again — and fetching a large config
# from a busy device is slow on its own. A client timeout shorter than the work
# leaves the change applied and the caller told it failed, which is the worst
# possible outcome for a write.
api = Backend(os.environ.get("AUTOMATION_URL", "http://automation:8100"), timeout=300.0)
mcp = build("netmiko")

ALLOW_WRITE = os.environ.get("MCP_ALLOW_WRITE", "false").lower() == "true"
WRITE_MODE = (os.environ.get("WRITE_MODE") or "approved").strip().lower()
if WRITE_MODE not in ("approved", "direct"):
    raise SystemExit(f"WRITE_MODE must be approved or direct (got {WRITE_MODE!r})")

# The audience of the tokens that may approve. Only the approvals app's own
# sign-in obtains it; an agent's token carries the MCP audiences instead. If
# MCP tools accepted it too, an agent's token could approve its own change.
APPROVALS_AUDIENCE = os.environ.get("CHANGE_APPROVALS_AUDIENCE", "").strip() or "darqcube-approvals"
APPROVE_ROLE = os.environ.get("CHANGE_APPROVE_ROLE", "").strip() or "darqcube-approve"
if WRITE_MODE == "approved" and APPROVALS_AUDIENCE in OIDC_AUDIENCES:
    raise SystemExit(f"MCP_OIDC_AUDIENCES must not include the approvals audience {APPROVALS_AUDIENCE!r}: "
                     "an agent's token could then approve")

CHANGE_ID = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}$")


@mcp.tool()
def get_device_config(device: str) -> dict:
    """Fetch the running configuration of a device.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/config")
    return {
        "device": device,
        "platform": result["platform"],
        "lines": result["lines"],
        "config": result["config"],
    }


@mcp.tool()
def get_device_state(device: str) -> dict:
    """Get parsed operational state from a device — interfaces and their status.

    Returns structured rows parsed from the device's CLI output, not raw text.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/state")
    return {
        "device": device,
        "platform": result["platform"],
        "command": result["command"],
        "rows": result.get("rows", []),
    }


# --- changes ---------------------------------------------------------------

def _actor(who: dict) -> dict:
    return {k: who.get(k) for k in ("user", "sub", "roles", "via", "agent", "auth_time")}


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def _summary(change: dict) -> dict:
    """What a model or a person needs from a change — never the archived config."""
    keys = ("id", "device", "state", "lines", "rollback", "fingerprint", "source", "requester", "requester_agent",
            "approver", "committer", "reject_reason", "error")
    out = {k: change.get(k) for k in keys if change.get(k) not in (None, "")}
    for k in ("staged_at", "approve_by", "decided_at", "commit_by", "committed_at", "rollback_used_at"):
        if change.get(k):
            out[k] = _iso(change[k])
    if change.get("result"):
        out["lines_sent"] = change["result"].get("lines_sent")
        out["previous_config_archived_to"] = change["result"].get("config_archived_to")
    return out


class _Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def _call(method: str, path: str, body: dict | None = None):
    """The automation API, with its refusal reason passed on as the message."""
    try:
        return api.post(path, json=body) if method == "POST" else api.get(path)
    except RuntimeError as exc:
        text = str(exc)
        match = re.search(r"-> (\d{3}): (.*)$", text, re.S)
        if not match:
            raise
        status, rest = int(match.group(1)), match.group(2)
        try:
            detail = json.loads(rest).get("detail", rest)
        except ValueError:
            detail = rest
        raise _Refused(status, detail if isinstance(detail, str) else json.dumps(detail)) from None


def _change_id(value: str) -> str:
    if not CHANGE_ID.fullmatch(value or ""):
        raise ValueError(f"invalid change id: {value!r}")
    return value


if ALLOW_WRITE and WRITE_MODE == "direct":
    # --- the one write tool, direct mode -----------------------------------
    # Registered ONLY when MCP_ALLOW_WRITE=true. Not "registered but refuses":
    # absent from tools/list entirely, so a caller that does not know about it
    # cannot discover it. Turning an AI loose on device configuration should be
    # a deliberate act, not a side effect of a container starting.

    @mcp.tool()
    def push_device_config(device: str, lines: list[str], ctx: Context) -> dict:
        """Push configuration lines to a device. THIS CHANGES THE NETWORK.

        The device's running configuration is archived before anything is sent.
        The caller must hold the write role (MCP_WRITE_ROLE) when calls carry a
        person's identity (MCP_AUTH_MODE=oidc).

        Args:
            device: the device name as it appears in the source of truth.
            lines: configuration lines to apply, in order.
        """
        identifier(device, "device")
        # Registration is the first gate (MCP_ALLOW_WRITE); this is the second:
        # the same tool, allowed for one person and refused for another.
        who = require_role(ctx)
        if not lines:
            raise ValueError("no configuration lines supplied")
        result = api.post(f"/device/{device}/config", json={"lines": lines})
        return {
            "device": device,
            "pushed_by": who["user"],
            "lines_sent": result["lines_sent"],
            "previous_config_archived_to": result["config_archived_to"],
            "output": result["output"],
        }


if ALLOW_WRITE and WRITE_MODE == "approved":
    # --- staged changes, approved mode -------------------------------------
    # Registered only with MCP_ALLOW_WRITE=true, like the direct tool. None of
    # these can approve: approval is a person's act on the /changes routes.

    def _tool_call(method: str, path: str, body: dict | None = None):
        try:
            return _call(method, path, body)
        except _Refused as exc:
            raise ValueError(f"refused: {exc}" if exc.status == 403 else str(exc)) from None

    @mcp.tool()
    def stage_device_change(device: str, lines: list[str], ctx: Context, rollback: list[str] | None = None,
                            source: str = "") -> dict:
        """Stage a configuration change for a person to approve. NOTHING IS SENT TO THE DEVICE.

        The change (device, the exact lines in order, the rollback) is stored and
        fingerprinted, and checked against protected lines. A person with the
        approve right approves or rejects it; then commit_device_change pushes it.
        Needs the propose right.

        Args:
            device: the device name as it appears in the source of truth.
            lines: configuration lines to apply, in order, one command each.
            rollback: the lines that undo the change, allowed once within an hour of the push.
            source: where the change was asked for, e.g. "chat" or "run 14" (max 64 characters).
        """
        identifier(device, "device")
        change = _tool_call("POST", "/changes", {"actor": _actor(caller(ctx)), "device": device, "lines": lines,
                                                 "rollback": rollback or [], "source": source[:64]})
        out = _summary(change)
        out["next"] = ("Waiting for a person with the approve right. Once approved, commit it with "
                       "commit_device_change within the commit window. You cannot approve it.")
        return out

    @mcp.tool()
    def get_device_change(change_id: str) -> dict:
        """A staged change and where it stands: waiting, approved, committed, rejected, withdrawn, expired or failed.

        Args:
            change_id: the id stage_device_change returned, e.g. 7f3a-c21e.
        """
        return _summary(_tool_call("GET", f"/changes/{_change_id(change_id)}"))

    @mcp.tool()
    def commit_device_change(change_id: str, ctx: Context) -> dict:
        """Push an APPROVED change to its device. THIS CHANGES THE NETWORK.

        Refused unless a person approved this exact change, within its window,
        and it was not used before. The device's running configuration is
        archived first. Needs the commit right, and to be the person who asked or
        the approver (or the right to commit any approved change).

        Args:
            change_id: the id stage_device_change returned.
        """
        return _summary(_tool_call("POST", f"/changes/{_change_id(change_id)}/commit",
                                   {"actor": _actor(caller(ctx))}))

    @mcp.tool()
    def rollback_device_change(change_id: str, ctx: Context) -> dict:
        """Push a committed change's declared rollback. THIS CHANGES THE NETWORK.

        Allowed once, within an hour of the push, by whoever may commit the change.

        Args:
            change_id: the id of a committed change that declared a rollback.
        """
        return _summary(_tool_call("POST", f"/changes/{_change_id(change_id)}/rollback",
                                   {"actor": _actor(caller(ctx))}))

    @mcp.tool()
    def withdraw_device_change(change_id: str, ctx: Context) -> dict:
        """Withdraw a change that is still waiting for approval. Only the person who asked.

        Args:
            change_id: the id stage_device_change returned.
        """
        return _summary(_tool_call("POST", f"/changes/{_change_id(change_id)}/withdraw",
                                   {"actor": _actor(caller(ctx))}))


# --- the approvals path: /changes, for people -------------------------------
# Served WITHOUT the MCP bearer check (PUBLIC_ROUTES) because each route
# authorises itself, with the approvals audience only: the token an approvals
# app gets from its own sign-in. An agent's token (the MCP audiences) is
# refused here, and an approvals token is refused by the MCP tools.

_approvals: Verifier | None = None


def _approvals_verifier() -> Verifier | None:
    global _approvals
    if _approvals is None and OIDC_ISSUER and OIDC_JWKS_URL:
        _approvals = Verifier(mode="oidc", issuer=OIDC_ISSUER, jwks_url=OIDC_JWKS_URL, audiences=[APPROVALS_AUDIENCE])
    return _approvals


async def _person(request: Request) -> dict:
    verifier = _approvals_verifier()
    if verifier is None:
        raise _Refused(503, "approvals need a person's sign-in: set MCP_AUTH_MODE=oidc or both, with the issuer")
    try:
        return await anyio.to_thread.run_sync(verifier.verify, request.headers.get("authorization", ""))
    except AuthError as exc:
        log.warning("audit denied server=netmiko route=%s reason=%s", request.url.path, exc)
        raise _Refused(401, "unauthorized") from None


def _route(handler):
    async def wrapped(request: Request):
        try:
            who = await _person(request)
            result = await handler(request, who)
            log.info("audit user=%s sub=%s via=approvals agent=%s server=netmiko route=%s %s", who["user"], who["sub"],
                     who.get("agent") or "-", request.method, request.url.path)
            return JSONResponse(result)
        except _Refused as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return wrapped


def _approver(who: dict) -> bool:
    return APPROVE_ROLE in who["roles"]


def _thread(*args):
    return anyio.to_thread.run_sync(lambda: _call(*args))


async def _list(request: Request, who: dict) -> dict:
    state = request.query_params.get("state", "")
    if state and not re.fullmatch(r"[a-z]{1,16}", state):
        raise ValueError(f"invalid state: {state!r}")
    limit = min(max(int(request.query_params.get("limit", "100")), 1), 500)
    found = (await _thread("GET", f"/changes?limit={limit}" + (f"&state={state}" if state else "")))["changes"]
    if not _approver(who):  # a person without the approve right sees their own changes only
        found = [c for c in found if c.get("requester_sub") == who["sub"]]
    return {"changes": [_summary(c) for c in found], "can_approve": _approver(who)}


async def _events(request: Request, who: dict) -> dict:
    if not _approver(who):
        raise _Refused(403, "the audit is for people with the approve right")
    limit = min(max(int(request.query_params.get("limit", "200")), 1), 1000)
    return await _thread("GET", f"/changes/events?limit={limit}")


async def _get(request: Request, who: dict) -> dict:
    change = await _thread("GET", f"/changes/{_change_id(request.path_params['change_id'])}")
    if not _approver(who) and change.get("requester_sub") != who["sub"]:
        raise _Refused(404, "no such change")
    return _summary(change)


def _acting(action: str):
    async def act(request: Request, who: dict) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        reason = str((body or {}).get("reason", ""))[:300]
        change_id = _change_id(request.path_params["change_id"])
        return _summary(await _thread("POST", f"/changes/{change_id}/{action}", {"actor": _actor(who), "reason": reason}))
    return act


PUBLIC_ROUTES = [
    Route("/changes", _route(_list), methods=["GET"]),
    Route("/changes/events", _route(_events), methods=["GET"]),
    Route("/changes/{change_id}", _route(_get), methods=["GET"]),
    Route("/changes/{change_id}/approve", _route(_acting("approve")), methods=["POST"]),
    Route("/changes/{change_id}/reject", _route(_acting("reject")), methods=["POST"]),
    Route("/changes/{change_id}/withdraw", _route(_acting("withdraw")), methods=["POST"]),
] if WRITE_MODE == "approved" else []
