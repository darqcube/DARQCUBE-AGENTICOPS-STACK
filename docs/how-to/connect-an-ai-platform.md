# Connect an AI platform

The stack works standalone — Grafana, Prometheus, Loki and the automation API
are all usable by a person with no AI involved. An AI platform is an
**additional** consumer, and there are three seams to connect it through.

## 1. Tools in — the MCP servers

Seven servers, all reached over the Docker network:

| Server | Address | Gives the AI |
|---|---|---|
| `mcp-infrahub` | `http://mcp-infrahub:9001/mcp` | what should exist |
| `mcp-prometheus` | `http://mcp-prometheus:9002/mcp` | metrics, alerts, flows |
| `mcp-loki` | `http://mcp-loki:9003/mcp` | logs |
| `mcp-grafana` | `http://mcp-grafana:9004/mcp` | dashboards to link to |
| `mcp-netmiko` | `http://mcp-netmiko:9005/mcp` | device config and parsed state |
| `mcp-assurance` | `http://mcp-assurance:9006/mcp` | assurance checks, snapshots, parsed config |
| `mcp-pyats` | `http://mcp-pyats:9007/mcp` | Genie structured models — BGP neighbors, interfaces, LLDP, platform |

`mcp-infrahub` tools — fixed queries, each taking at most one validated name:

| Tool | Answers |
|---|---|
| `list_devices`, `get_device(device)`, `get_site_devices(site)` | which devices exist, where, and how they are reached |
| `list_applications` | every application with its criticality and the hosts it runs on |
| `get_application(application)` | its services (protocol, port), hosts, sites, owner |
| `get_host(host)` | address, site, subnet and gateway device, and the services it runs |
| `get_site_services(site)` | the hosts at a site and the applications they serve |
| `get_application_dependencies(application)` | the hosts, subnets, gateways and site devices an application depends on — "what breaks if X fails?" |

`mcp-pyats` tools — a feature is a name from the platform's `pyats:` allow-list
in `platforms.yml`, never a Genie command:

| Tool | Answers |
|---|---|
| `list_pyats_features(device)` | which Genie features this device's platform has (`learn` or `parse`) |
| `learn_device_feature(device, feature)` | Genie's structured model — `ok`, `absent` (not configured) or `error` |
| `get_bgp_neighbors(device)` | every BGP session as VRF, address family, peer and state |

The application tools return empty results until hosts and services are
modelled — [model-applications.md](model-applications.md). With services
modelled, `get_flow_summary` on `mcp-prometheus` also returns `by_application`.

Transport is streamable-HTTP; auth is a bearer credential:

```
Authorization: Bearer <credential>
```

`MCP_AUTH_MODE` decides what the credential is:

| Mode | Credential | Use when |
|---|---|---|
| `token` (default) | the shared `MCP_AUTH_TOKEN` | one trusted client; no per-person identity |
| `oidc` | the **caller's own JWT** from the ai-platform's identity provider, checked against its JWKS (issuer, audience, expiry) | the ai-platform has sign-in and forwards the user's token — every call is attributable to a person, and roles gate the write tool |
| `both` | either | moving from one to the other, or keeping `make mcp-check` working with the token |

Every request is logged with who made it and which tool it called (`docker compose logs mcp-netmiko | grep audit`):

```
audit user=alice sub=4f1c… via=oidc server=netmiko rpc=tools/call tool=get_device_state
```

### If the AI platform runs in the same Compose project

Attach it to the `darqcube` network and use the service names above. Nothing to
publish.

### If it runs on another machine

By default the ports are published on `127.0.0.1` only. Opening them to an
ai-platform on another host takes three values that live **on the ai-platform
side** — its identity provider's issuer, the URL its signing keys can be fetched
from, and the address this stack reaches that host on — so they are generated
there rather than typed on this VM. Every value differs per deployment; the
examples below use documentation addresses (`192.0.2.10` for this stack,
`192.0.2.1` for the ai-platform host).

**1. On the ai-platform host**, from a clone of this repository:

```bash
python3 scripts/ai-platform-connect.py \
    --stack-host 192.0.2.10 \
    --idp http://localhost:7080/realms/<realm> \
    --token-header <header the ai-platform forwards the caller's token in> \
    --max-response-kb 64
```

It reads the provider's OpenID configuration, works out this machine's address
on the route to the stack, and writes:

| File | What it is | Goes to |
|---|---|---|
| `ai-platform.site.yml` | the `ai_platform:` block — publish, auth mode, issuer, JWKS URL, audiences | the stack VM, as `sites/ai-platform.yml` |
| `mcp-servers.snippet.yaml` | the seven servers with their endpoints and credential source | the ai-platform's MCP server configuration |

The issuer is kept exactly as tokens carry it (often `http://localhost:…`); only
the JWKS URL is rewritten to an address the stack can reach. Add
`--token-file user.jwt` to check a real token's `iss`, audiences and roles.

**2. Copy it to the stack VM** (both files are gitignored — they name your
addresses):

```bash
ssh <user>@192.0.2.10 mkdir -p ~/DARQCUBE-AGENTICOPS-STACK/sites
scp ai-platform.site.yml <user>@192.0.2.10:~/DARQCUBE-AGENTICOPS-STACK/sites/ai-platform.yml
```

**3. On the stack VM**, apply it:

```bash
cd ~/DARQCUBE-AGENTICOPS-STACK
git pull
python3 install.py --step 2      # merges sites/ai-platform.yml into .env
make mcp-apply                   # rebuild and recreate the MCP servers
make mcp-check                   # 7 servers, tools listed (uses the shared token: mode both, or token)
```

`install.py` merges the file over `site.yml` if you have one, or — for an install
done without a site file — changes only the MCP keys in the existing `.env`. The
file may only contain `ai_platform`; it cannot touch credentials or ports. Check
the VM can fetch the keys: `curl -s <jwks_url>`.

**4. Register the servers** in the ai-platform from `mcp-servers.snippet.yaml`,
then from the ai-platform host:

```bash
python3 scripts/mcp-check.py --host 192.0.2.10 --token-file user.jwt
```

Prefer `site.yml` by hand? The same keys are documented in `site.example.yml`
under `ai_platform.publish` and `ai_platform.auth`.

Publishing is for a trusted network. There is no TLS between components (see
[INSTALL](../INSTALL.md)); put a reverse proxy with TLS in front before exposing
the ports any further.

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
make mcp-check
# netmiko: 2 tools = read only.  3 tools = push_device_config is live.
```

When enabled it archives the running config before sending anything, caps the
change at `MAX_CONFIG_LINES`, and logs every push with the device and the lines.

### What it does **not** do

It gates the **MCP tool**, not the HTTP API underneath. The automation API has
no authentication of its own, so anything that can reach port 8100 can call it
directly regardless of the flag:

```bash
curl -X POST http://vm:8100/device/router1/config -d '{"lines":["..."]}'
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

- **Identity.** With `MCP_AUTH_MODE=oidc` each call carries the person's own
  token and the audit log names them. In `token` mode all seven servers share one
  `MCP_AUTH_TOKEN` and the log can only say "token".
- **Per-tool authorisation.** `MCP_ALLOW_WRITE` is one flag over one tool.
  Finer control — this agent may read logs, that one may run checks — belongs
  in the platform, or in a registry in front of the servers.
- **Approval on writes.** There is no human-in-the-loop step. A push happens
  when the tool is called by someone holding `MCP_WRITE_ROLE` (in `oidc` mode).
  If your platform has an approval concept, that is where it belongs.
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
make mcp-check
```

A real MCP handshake against every server, listing its tools.
