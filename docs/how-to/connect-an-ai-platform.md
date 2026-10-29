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

### Graphs in the chat (optional)

`ai_platform.graphs: true` (or `--graphs` on `scripts/ai-platform-connect.py`)
starts Grafana's image renderer (container `grafana-renderer`, profile
`graphs`) and registers two more tools on `mcp-grafana`:

| Tool | Answers |
|---|---|
| `render_interface_graph(device, interface, window, kind)` | a PNG of one interface's **traffic** or **errors** over the window, as a Markdown image link, plus the same numbers as text |
| `render_link_comparison(link_a, link_b, kind, window)` | two groups of links side by side, by interface description: **traffic_share** (two pies), or IP SLA **availability**, **rtt**, **jitter** (grouped bars per device) — [per-link probes](../devices/cisco-ios-xe.md#3b-ip-sla--wan-quality-and-application-response-time-optional) |

How the picture reaches the chat: chat platforms drop images inside tool
results, so the tool keeps the PNG in memory and returns
`![cr2 Et0/1 traffic, last 1h](http://192.0.2.10:9004/g/cr2-et0-1-traffic-1h-k7m2x9q4ab.png)`.
The model puts that line in its answer; the user's browser loads the image. The
link is its own authorisation, because an `<img>` cannot send a bearer token:
a readable label (models copy words reliably, and mangle long random strings)
plus a 50-bit random suffix, forgotten after 15 minutes (`MCP_IMAGE_TTL_SECONDS`).
After that, or after the server restarts (images are kept in memory only), the
link shows a small "Graph expired" image instead of a broken one. Ask again for
a fresh graph — and tell your agents to call `render_interface_graph` for every
graph request, never re-using a link from earlier in the chat.

- It renders panel 1 or 2 of the **Interface Detail** dashboard only, from
  validated arguments; a caller never names a dashboard, panel or query.
- Images come from `MCP_IMAGE_BASE_URL` (`http://<published address>:9004`).
  In production put an HTTPS reverse proxy in front and set that variable to
  it: platforms that enforce a Content-Security-Policy load `https:` images only.
- Tell the agent to paste the tool's `markdown` field verbatim — a model that
  rewrites the URL breaks the image (the `summary` and `dashboard_url` still work).
- Renders are headless Chromium: 2–8 s each, at most 4 at once, the container
  capped at 1.5 GB.

Apply on the stack host: `python3 install.py --step 2 && make mcp-apply && make up`.

**Turning graphs off** (`graphs: false`, or no `--graphs`; e.g. when the AI platform
draws its own charts from the metrics): the same three commands. `make mcp-apply`
unregisters both render tools, and `make up` stops and removes `grafana-renderer`:
it removes every service whose profile is no longer in `COMPOSE_PROFILES` (Compose
itself would leave it running). Grafana, its dashboards and `get_dashboard_link`
stay.

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

Or let the AI platform host generate it with the rest of its settings:
`--alert-webhook-url <url>` on `scripts/ai-platform-connect.py` writes
`ai_platform.alert_webhook_url` into the overlay, and `install.py` applies it
(an empty string stops delivery; leaving the option out keeps the current
value). The URL may carry a path secret, so the overlay stays gitignored.

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
AI platform ──a person's token──> mcp-netmiko ──compose network──> automation API ──SSH──> devices
                                      ▲                              (loopback only)          ▲
                         MCP_ALLOW_WRITE: change tools exist?                    device account privilege
                         WRITE_MODE=approved: a person approves each change      gates what ANY caller can do
```

### `MCP_ALLOW_WRITE` — whether the change tools exist

Off by default:

```bash
MCP_ALLOW_WRITE=false     # no change tool is REGISTERED
```

The tools are absent from `tools/list` entirely — not present and refusing.
A caller that cannot discover a tool cannot call it.

**Only the stack owner switches it, on the stack host.** The ai-platform's
connection file cannot: `install.py` ignores an `allow_write` in it, and
`scripts/ai-platform-connect.py` no longer writes one (`--allow-write` is
ignored with a note). To enable, and back:

```bash
make writes-on    # MCP_ALLOW_WRITE=true in .env, recreates mcp-netmiko, shows its tools
make writes-off   # read only again
```

With a site file, also set `ai_platform.allow_write` there: `install.py`
rebuilds `.env` from it (`make writes-on` reminds you when they differ).

Confirm which you have:

```bash
make mcp-check
# netmiko: 2 tools = read only.  7 = approved changes (stage, commit…).  3 = push_device_config (direct).
```

### `WRITE_MODE` — how a change reaches a device

**`approved` (the default):** the agent stages a change, a person with the
approve right approves exactly that change through a path an agent's token
cannot use, and the agent commits it once, within a short window. The rules,
the rights (from your identity provider's groups) and the approvals routes for
an approvals app: [approve-changes.md](approve-changes.md).

**`direct`:** the old single tool, `push_device_config`, gated only by the
caller's `MCP_WRITE_ROLE`. For a deployment that approves changes elsewhere;
unsafe for an AI, which could push on a prompt alone.

Either way the running config is archived before anything is sent, the change
is capped at `MAX_CONFIG_LINES`, and every action is logged with the person
(and the agent, `MCP_AGENT_CLAIM`).

### The automation API underneath

It has no authentication of its own, and in approved mode it trusts the person
mcp-netmiko verified. So it is published on **loopback only**
(`AUTOMATION_BIND_IP=127.0.0.1`, the default) and refuses a direct
`POST /device/{name}/config` unless `WRITE_MODE=direct`. This costs the AI
platform nothing: the MCP servers reach it over the Docker network.

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

| Scenario | `MCP_ALLOW_WRITE` | `WRITE_MODE` | Device account | `AUTOMATION_BIND_IP` |
|---|---|---|---|---|
| Demo on your own machine | either | either | either | either |
| PoC at a customer, read-only | `false` | — | **read-only** | loopback (default) |
| AI reads, humans change | `false` | — | privileged | loopback (default) |
| AI reads and changes | `true` | **`approved`** (default) | privileged | **loopback** (default) — every change then needs a person's approval |

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
