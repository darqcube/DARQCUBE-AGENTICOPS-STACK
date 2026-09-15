# MCP servers

## What they do here

Expose the rest of the stack to an AI platform as bounded tools. Six containers
from **one image**; `MCP_SERVER` picks the module, `PORT` picks the port.

**Optional by design.** Drop `mcp` from `COMPOSE_PROFILES` and nothing else
loses a feature — nothing in the stack depends on them.

| Server | Port | Backend | Tools |
|---|---|---|---|
| `mcp-infrahub` | 9001 | Infrahub | 3 |
| `mcp-prometheus` | 9002 | Prometheus | 5 |
| `mcp-loki` | 9003 | Loki | 3 |
| `mcp-grafana` | 9004 | Grafana | 2 |
| `mcp-netmiko` | 9005 | automation API | 2 (3 with writes on) |
| `mcp-assurance` | 9006 | automation API | 4 |

## Configuration

| Where | What |
|---|---|
| `mcp/common.py` | transport, auth, argument bounds |
| `mcp/servers/*.py` | one module per server |
| `mcp/serve.py` | entry point |
| `.env` → `MCP_AUTH_TOKEN` | bearer token, all six |
| `.env` → `MCP_ALLOW_WRITE` | whether an AI may change device config |

## Not published

All six are `expose:` only, reached over the Docker network at
`http://mcp-prometheus:9002/mcp`. A bearer token is the only thing in front of
them, so publishing them needs a reverse proxy with TLS —
[../how-to/connect-an-ai-platform.md](../how-to/connect-an-ai-platform.md).

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
source .env
for p in 9001 9002 9003 9004 9005 9006; do
  docker compose exec -T automation curl -sf "http://mcp-prometheus:$p/healthz" 2>/dev/null
done

docker compose exec automation curl -s \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -X POST http://mcp-netmiko:9005/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

With writes off, `push_device_config` must not appear. A test checks this.

```bash
.venv/bin/python -m pytest automation/tests/test_mcp.py -v
```

## Problems

| Symptom | Cause |
|---|---|
| 401 on every call | missing or wrong `Authorization: Bearer` header |
| 406 / stream errors | add `Accept: application/json, text/event-stream` |
| A server exits at start | `MCP_SERVER` is not one of the six valid names |
| `mcp-netmiko` tools fail | the automation container is down — they front its API |
| `push_device_config` missing | `MCP_ALLOW_WRITE` is not `true` (this is the default, and intended) |
