#!/usr/bin/env python3
"""List every MCP server's tools, the way an MCP client does.

    python3 scripts/mcp-check.py                       # on the stack host, shared token from .env
    python3 scripts/mcp-check.py --host 192.0.2.10 --token-file user.jwt   # from the ai-platform host
    python3 scripts/mcp-check.py --heal                # recreate servers running without their port, then check

A real handshake per server — initialize, initialized, tools/list — so a pass
means a client can actually use it. A healthy /healthz proves much less: the
servers once answered it while refusing every MCP call with 421.

Standard library only. Exit status 1 if any server fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVERS = [("infrahub", 9001), ("prometheus", 9002), ("loki", 9003), ("grafana", 9004),
           ("netmiko", 9005), ("assurance", 9006), ("pyats", 9007)]


def env_value(key: str) -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].split("#")[0].strip()
    return ""


def env_token() -> str:
    return env_value("MCP_AUTH_TOKEN")


def post(url: str, payload: dict, headers: dict) -> tuple[int, dict, list[dict]]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode()
            status, got = response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), [{"error": exc.read().decode()[:200]}]
    if got.get("Content-Type", "").startswith("application/json"):
        return status, got, [json.loads(body)] if body else []
    return status, got, [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:")]


def unpublished(name: str) -> bool:
    """On the stack host: the server's container runs but Docker published no port for it. That happens when Docker
    starts it before MCP_BIND_IP exists on the host (a reboot): the container is healthy, the port is simply absent."""
    try:
        out = subprocess.run(["docker", "compose", "ps", "--format", "json", f"mcp-{name}"], cwd=ROOT,
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False   # not on the stack host (no docker here): nothing to say
    text = out.stdout.strip()   # newer Compose: one JSON object per line; older: one JSON array
    rows = json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.startswith("{")]
    return any(r.get("State") == "running" and not any(p.get("PublishedPort") for p in r.get("Publishers") or [])
               for r in rows)


def check(host: str, name: str, port: int, token: str) -> tuple[bool, str]:
    url = f"http://{host}:{port}/mcp"
    auth = {"Authorization": f"Bearer {token}"}
    try:
        status, headers, _ = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "mcp-check", "version": "1"}}}, auth)
    except OSError as exc:
        if unpublished(name):
            return False, "running, but its port is not published (started before MCP_BIND_IP existed?): make mcp-heal"
        return False, f"unreachable: {exc}"
    if status != 200:
        hint = {401: "token refused", 421: "Host not allowed — MCP_ALLOWED_HOSTS"}.get(status, "")
        return False, f"initialize -> {status} {hint}".strip()
    session = {**auth, "mcp-session-id": headers.get("mcp-session-id", "")}
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    status, _, messages = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    tools = [t["name"] for m in messages for t in (m.get("result") or {}).get("tools", [])]
    if status != 200 or not tools:
        return False, f"tools/list -> {status} {messages[:1]}"
    return True, f"{len(tools)} tools: {', '.join(tools)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default=env_value("MCP_BIND_IP") or "127.0.0.1",
                    help="where the MCP ports are published (default: MCP_BIND_IP from .env)")
    ap.add_argument("--token", help="bearer credential (default: MCP_AUTH_TOKEN from .env)")
    ap.add_argument("--token-file", type=Path, help="read the bearer credential from a file, e.g. a user JWT")
    ap.add_argument("--heal", action="store_true",
                    help="on the stack host: recreate the servers running without their published port, then check")
    args = ap.parse_args()
    if args.heal:
        missing = [f"mcp-{name}" for name, _ in SERVERS if unpublished(name)]
        if missing:
            print(f"recreating, port not published: {' '.join(missing)}")
            subprocess.run(["docker", "compose", "up", "-d", "--wait", "--force-recreate", *missing], cwd=ROOT, check=False)
        else:
            print("every MCP server has its published port")
    token = args.token_file.read_text().strip() if args.token_file else (args.token or env_token())
    if not token:
        print("no credential: pass --token / --token-file, or run where .env has MCP_AUTH_TOKEN")
        return 2

    failed = 0
    for name, port in SERVERS:
        passed, detail = check(args.host, name, port, token)
        failed += not passed
        print(f"{'ok  ' if passed else 'FAIL'}  mcp-{name:<11} :{port}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
