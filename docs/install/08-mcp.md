# MCP servers

## What they do here

Expose the rest of the stack to an AI platform as bounded tools. Seven containers
from **one image**; `MCP_SERVER` picks the module, `PORT` picks the port.

**Optional by design.** Drop `mcp` from `COMPOSE_PROFILES` and nothing else
loses a feature — nothing in the stack depends on them.

| Server | Port | Backend | Tools |
|---|---|---|---|
| `mcp-infrahub` | 9001 | Infrahub | 8 |
| `mcp-prometheus` | 9002 | Prometheus | 5 |
| `mcp-loki` | 9003 | Loki | 3 |
| `mcp-grafana` | 9004 | Grafana | 2 |
| `mcp-netmiko` | 9005 | automation API | 2 (3 with writes on) |
| `mcp-assurance` | 9006 | automation API | 4 |
| `mcp-pyats` | 9007 | automation API (pyATS/Genie) | 3 |

## Configuration

| Where | What |
|---|---|
| `mcp/common.py` | transport, auth, argument bounds |
| `mcp/servers/*.py` | one module per server |
| `mcp/serve.py` | entry point |
| `.env` → `MCP_AUTH_TOKEN` | shared bearer token, all seven |
| `.env` → `MCP_AUTH_MODE` | `token` (shared token), `oidc` (each caller's own JWT), or `both` |
| `.env` → `MCP_ALLOW_WRITE` | whether an AI may change device config |
| `.env` → `MCP_WRITE_ROLE` | the role an `oidc` caller needs for the write tool |
| `.env` → `MCP_BIND_IP` / `MCP_ALLOWED_HOSTS` | where the ports are published, and the Host headers answered |
| `.env` → `MCP_MAX_RESPONSE_BYTES` | cap on one tool result — lower it for small local models |

## This host only, unless published

All seven are reached over the Docker network (`http://mcp-prometheus:9002/mcp`)
and published on `MCP_BIND_IP`, which defaults to `127.0.0.1` — this host only.
An ai-platform on another machine needs them published on this host's network
address, and the servers told to answer to it: both come from
`ai_platform.publish` in `site.yml`, normally generated on the ai-platform host —
[../how-to/connect-an-ai-platform.md](../how-to/connect-an-ai-platform.md).

The MCP SDK refuses any request whose `Host` header is not on the server's
allow-list, with **421**. Loopback and the compose names are always on it;
`MCP_ALLOWED_HOSTS` adds the published address.

## No passthrough tools

No tool takes a raw PromQL, LogQL, GraphQL or CLI string. Every query is
composed server-side from validated arguments.

This is not conservatism. One passthrough tool makes every other boundary in the
stack decorative, and it is very hard to remove once something depends on it. If
a question cannot be answered by the existing tools, add a **specific** tool for
it.

A test enforces this by inspecting every tool signature.

## The one write tool

`push_device_config` on `mcp-netmiko` is the only tool that changes anything.

```bash
MCP_ALLOW_WRITE=false     # default — the tool is NOT REGISTERED
```

With the default it is absent from `tools/list` entirely. Not "present but
refuses" — absent, so a caller that does not know about it cannot discover it.
Turning an AI loose on device configuration should be a deliberate act, not a
side effect of a container starting.

```bash
MCP_ALLOW_WRITE=true
docker compose up -d --force-recreate mcp-netmiko
```

With `MCP_AUTH_MODE=oidc` there is a second gate: the caller's token must carry
`MCP_WRITE_ROLE`. The same tool is then allowed for one person and refused for
another, and every push records who made it.

It archives the running config before sending and is capped by
`MAX_CONFIG_LINES`.

**It gates the MCP tool, not the API underneath.** The automation API has no
authentication of its own, so anything that can reach port 8100 can push
configuration regardless of this flag. Unpublishing that port closes the bypass
and costs the AI nothing — the MCP servers reach the API over the Docker
network. See
[../how-to/connect-an-ai-platform.md](../how-to/connect-an-ai-platform.md#what-it-does-not-do).

## Verify

```bash
make mcp-check
```

A real client handshake against each server — initialize, then `tools/list` —
not just `/healthz`, which answers even when every MCP call is refused. With
writes off, `push_device_config` must not appear in the netmiko line.

```bash
.venv/bin/python -m pytest automation/tests/test_mcp.py automation/tests/test_mcp_transport.py -v
```

## Problems

| Symptom | Cause |
|---|---|
| 401 on every call | missing or wrong bearer credential — the shared token in `token` mode, a valid JWT from the configured issuer in `oidc` mode (`docker compose logs mcp-netmiko` says why) |
| 421 Misdirected Request | the `Host` the client used is not allowed — set `ai_platform.publish` so `MCP_ALLOWED_HOSTS` names this host's address |
| 406 / stream errors | add `Accept: application/json, text/event-stream` |
| A server exits at start | `MCP_SERVER` is not one of the seven valid names, `MCP_AUTH_TOKEN` is empty, or `oidc` mode lacks `MCP_OIDC_ISSUER` / `MCP_OIDC_JWKS_URL` |
| `mcp-netmiko` / `mcp-pyats` tools fail | the automation container is down — they front its API |
| `push_device_config` says the caller lacks a role | `oidc` mode and the user's token has no `MCP_WRITE_ROLE` — intended |
| `push_device_config` missing | `MCP_ALLOW_WRITE` is not `true` (this is the default, and intended) |
