# Connect an AI platform

The stack works standalone — Grafana, Prometheus, Loki and the automation API
are all usable by a person with no AI involved. An AI platform is an
**additional** consumer, and there are three seams to connect it through.

## 1. Tools in — the MCP servers

Six servers, all reached over the Docker network:

| Server | Address | Gives the AI |
|---|---|---|
| `mcp-infrahub` | `http://mcp-infrahub:9001/mcp` | what should exist |
| `mcp-prometheus` | `http://mcp-prometheus:9002/mcp` | metrics, alerts, flows |
| `mcp-loki` | `http://mcp-loki:9003/mcp` | logs |
| `mcp-grafana` | `http://mcp-grafana:9004/mcp` | dashboards to link to |
| `mcp-netmiko` | `http://mcp-netmiko:9005/mcp` | device config and parsed state |
| `mcp-assurance` | `http://mcp-assurance:9006/mcp` | assurance checks, snapshots, parsed config |

Transport is streamable-HTTP; auth is a bearer token:

```
Authorization: Bearer ${MCP_AUTH_TOKEN}
```

### If the AI platform runs in the same Compose project

Attach it to the `darqcube` network and use the service names above. Nothing to
publish.

### If it runs elsewhere

Publish the ports you need, in an override file rather than by editing
`compose/mcp.yaml`:

```yaml
# compose.override.yaml
services:
  mcp-prometheus:
    ports: ["9002:9002"]
  mcp-loki:
    ports: ["9003:9003"]
```

They are unpublished by default because a bearer token is the only thing in
front of them. Put them behind a reverse proxy with TLS before exposing them
beyond a trusted network.

## 2. Alerts out — the Alertmanager webhook

```bash
# .env
ALERT_WEBHOOK_URL=https://your-ai-platform.example/api/alerts
```

```bash
docker compose up -d --force-recreate config-init alertmanager
```

Every alert is then POSTed as JSON, including resolutions. This is a push, so
the AI platform learns about a problem without polling.

## 3. Actions — the automation API

`http://automation:8100`, or `localhost:${AUTOMATION_PORT}` from the host. The
same endpoints `mcp-netmiko` and `mcp-assurance` front, if you prefer plain HTTP
to MCP.

---

## Letting an AI change device configuration

### The chain, and what gates what

```
AI platform ──bearer token──> MCP servers ──no auth──> automation API ──SSH──> devices
                                   ▲                                              ▲
                          MCP_ALLOW_WRITE                             device account privilege
                          gates the AI's tool                         gates what ANY caller can do
```

Two controls, at different layers, covering different things. Knowing which is
which saves an unpleasant surprise later.

### `MCP_ALLOW_WRITE` — what it does

Off by default:

```bash
MCP_ALLOW_WRITE=false     # push_device_config is NOT REGISTERED
```

The tool is absent from `tools/list` entirely — not present and refusing.
A caller that cannot discover a tool cannot call it. To enable:

```bash
MCP_ALLOW_WRITE=true
docker compose up -d --force-recreate mcp-netmiko
```

Confirm which you have:

```bash
docker compose exec automation curl -s \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -X POST http://mcp-netmiko:9005/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 2 tools = read only.  3 tools = push_device_config is live.
```

When enabled it archives the running config before sending anything, caps the
change at `MAX_CONFIG_LINES`, and logs every push with the device and the lines.

### What it does **not** do

It gates the **MCP tool**, not the HTTP API underneath. The automation API has
no authentication of its own, so anything that can reach port 8100 can call it
directly regardless of the flag:

```bash
curl -X POST http://vm:8100/device/cr1/config -d '{"lines":["..."]}'
```

That is a different threat model — not "my AI did something unexpected" but
"something else on the network used the API directly". The flag was never meant
to cover it, but the name invites you to assume it does.

**This costs your AI platform nothing to fix.** The MCP servers reach the
automation API over the Docker network, so unpublishing the host port closes
the bypass while leaving the AI path fully working:

```yaml
# compose/automation.yaml
#   ports:
#     - "${AUTOMATION_PORT:-8100}:8100"      # comment out, or:
#     - "127.0.0.1:${AUTOMATION_PORT:-8100}:8100"
```

```bash
docker compose up -d --force-recreate automation
```

Your AI keeps working. You lose only `curl` from another machine, and
`make config-get` / `make state` from the host if you localhost-bind rather
than unpublish.

### The stronger control: the device account

`MCP_ALLOW_WRITE` is configuration you own. The device account privilege is
enforced **by the device**, which is why it survives a misconfiguration:

```
! Cisco IOS-XE
username darqcube privilege 5 secret <PASSWORD>      ! read-only
```

With a read-only account the stack physically cannot change anything, whatever
the flag says or who reaches the API. You keep every metric, log, parsed state
and assurance check. See
[deploy-on-customer-premises.md](deploy-on-customer-premises.md#3-start-with-a-read-only-device-account).

### Which combination to use

| Scenario | `MCP_ALLOW_WRITE` | Device account | Publish :8100 |
|---|---|---|---|
| Demo on your own machine | either | either | fine |
| PoC at a customer, read-only | `false` | **read-only** | close it |
| AI reads, humans change | `false` | privileged | close it |
| AI reads and changes | `true` | privileged | **close it** — the flag is then your only control |

"Close it" means unpublish or localhost-bind, as above.

---

## When you connect a production AI platform

What is here is built and tested; what a production integration needs on top
depends on the platform, and is deliberately not guessed at:

- **Identity.** Today all six servers share one `MCP_AUTH_TOKEN`. A platform
  with multiple agents would want a token per agent so an audit log can say
  *which* agent acted. The servers already stash `X-Agent-Id` from the request.
- **Per-tool authorisation.** `MCP_ALLOW_WRITE` is one flag over one tool.
  Finer control — this agent may read logs, that one may run checks — belongs
  in the platform, or in a registry in front of the servers.
- **Approval on writes.** There is no human-in-the-loop step. A push happens
  when the tool is called. If your platform has an approval concept, that is
  where it belongs.
- **Rate limiting.** Nothing stops an agent looping over 400 devices. The
  per-device lock serialises one device; it does not bound overall volume.

None of these block a read-only integration, which is where most platforms
start.

## What the tools deliberately do not do

No tool takes a raw PromQL, LogQL, GraphQL or CLI string. Every query is
composed server-side from validated arguments. This is not a limitation to work
around — one passthrough tool makes every other boundary in the stack
decorative, and it is very hard to remove once something depends on it.

If a question cannot be answered by the existing tools, add a **specific** tool
for it in `mcp/servers/<server>.py` rather than a general one.

## Check the surface

```bash
docker compose exec automation curl -s \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H 'Accept: application/json, text/event-stream' \
  -X POST http://mcp-prometheus:9002/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
