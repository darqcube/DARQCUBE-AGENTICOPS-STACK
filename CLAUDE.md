# CLAUDE.md

Instructions for Claude Code working in this repo. `README.md` is the document for humans — this one
exists so an agent doesn't rediscover the same constraints by breaking them.

## What this is

A single-VM Docker Compose stack that collects telemetry from network devices, holds their intended
state in Infrahub, automates get/put of configuration, and exposes all of it over MCP so an AI
platform can use it. Cisco IOS-XE, Huawei VRP, MikroTik RouterOS.

Device work is **Netmiko + TextFSM + TTP** for every vendor, with **pyATS/Genie added** wherever
Genie genuinely returns data. pyATS is additive — never a platform's only path.

Built for demos, PoCs and small production networks — **sized and tested to 400 devices** on one
Ubuntu box. It is deliberately not hardened: no TLS between components, no approval workflows, no
backup automation. Don't add them without being asked.

`python3 install.py` is the primary install path: one stdlib-only file, seven steps, ending in
pytest against the running stack. Keep it dependency-free — it runs on a freshly cloned repo before
pip has been used, and a test enforces that. That is also why it parses `site.yml` with its own
small reader rather than PyYAML; a test asserts the reader agrees with PyYAML on the example.

**`site.yml` is the per-deployment input** — one file per customer site, gitignored because it
holds credentials. It generates `.env` and nothing else: docs, compose and everything tracked stay
as cloned. Adding a setting means adding it to `site_to_env()` **and** `.env.example`, or it is
silently dropped — there is a test for that.

Four groups, each with its own compose file and top-level directory:

| Group | Directory | Compose file | Does |
|---|---|---|---|
| source-of-truth | `source-of-truth/` | `compose/source-of-truth.yaml` | Infrahub — what should exist |
| observability | `observability/` | `compose/observability.yaml` | Telegraf, Logstash, Prometheus, Loki, Alertmanager, Grafana |
| automation | `automation/` | `compose/automation.yaml` | Nornir, Netmiko, TextFSM, TTP, pyATS, assurance |
| mcp | `mcp/` | `compose/mcp.yaml` | seven servers, one image |

Telegraf ingests SNMP, gNMI and NetFlow/IPFIX. **Logstash ingests syslog** — not Telegraf. That
split is deliberate; don't merge them.

## Golden rules

- **Edit the YAML, not the output.** `observability/telegraf/generated/` is written by
  `render-inventory.py`; anything you put there is lost on the next `make render`.
- **Never hardcode an IP or hostname in a config file.** Everything addressable comes from `.env` or
  from Infrahub via the renderer. This repo gets cloned onto other people's VMs.
- **One credential pair** — `DEVICE_USER` / `DEVICE_PASSWORD` — for Nornir and Netmiko. Don't
  add per-tool variants; they drift apart and then nobody knows which one is real.
- **Device credentials never go in Infrahub.** `.env` only.
- **The inventory is per deployment, never in the repo** — gitignored like `site.yml`; `make seed`
  never reads `devices/examples/`.
- **This repo is public: nothing from a specific network or lab.** No real site, device or lab
  names, addresses or hostnames in code, docs, tests, samples or examples — and no references to a
  particular lab's topology, its layout or tool files, or how one lab is built. Examples use
  neutral names: devices `router1`, `router2`, `switch1`, `mt-01`; sites `hq`, `branch-01`;
  documentation addresses (`192.0.2.0/24`, `10.0.0.0/8`). Naming a tool as a way to *run* the stack
  (OrbStack, containerlab, Docker Desktop) is fine; describing one lab built with it is not.
  Before committing, search the change for every name and address of the networks you test
  against.
- **Adding a vendor is six edits**: `platforms.yml`, a Logstash grok file, a TextFSM template (if
  `ntc-templates` has none), a normaliser in `automation/assurance/normalise.py`, the `platform`
  dropdown in the Infrahub schema, and an onboarding doc. All six, or the vendor is
  half-supported — and half-support fails *silently*, which is worse than not supporting it.
  A `pyats:` block is optional and comes on top — declare only what Genie really has for that OS.

## Commands

Use the Makefile rather than inventing `docker compose` invocations.

| Command | Does |
|---|---|
| `./scripts/prepare-ubuntu.sh` | fresh Ubuntu host: OS packages, Docker, docker group — before `install.py`, needs sudo |
| `python3 install.py` | the whole install: preflight → configure → build → start → initialise → verify |
| `python3 install.py --from FILE` | generate `.env` from a site file (defaults to `./site.yml`) |
| `python3 install.py --check` | preflight only, changes nothing |
| `make up` | `up -d --wait` — blocks until every container is healthy |
| `make schema` / `make seed` | load the Infrahub schema / apply `devices/*.yml` |
| `make render` | Infrahub → Telegraf + Logstash configs. **Run after any device change.** |
| `make config-get DEV=x` / `config-put DEV=x FILE=y` | get and put device config |
| `make state DEV=x` | parsed operational state (TextFSM) |
| `make check DEV=x` | run the assurance rules |
| `make snapshot DEV=x` | comparable state, for pre/post comparison |
| `make config-parsed DEV=x` | running config parsed with TTP |
| `make test-templates` | TextFSM tests — offline, seconds, no devices |
| `make test` | stack tests — needs the stack up |
| `make test-devices` | needs real devices |
| `make verify` | exactly what `docs/INSTALL.md` says to verify |
| `make mcp-apply` | rebuild and recreate the seven MCP servers (after a pull or an `.env` change) |
| `make mcp-check` | a real MCP handshake + `tools/list` against every server |
| `python3 scripts/ai-platform-connect.py` | **on the ai-platform host**: generate `sites/ai-platform.yml` + the MCP server snippet |

## Scale — 400 devices is the tested ceiling

~105,000 Prometheus series at 60s polling. Prometheus is not the constraint; the collectors are.
Three settings exist because of that, and lowering any of them silently loses data:

| Setting | Why |
|---|---|
| `SNMP_SHARD_SIZE=150` | splits the fleet across several `[[inputs.snmp]]` gather loops, so one unreachable device cannot eat the poll window |
| `metric_buffer_limit = 250000` | one interval produces ~105k metrics; a smaller buffer drops the excess on any slow flush |
| `net.core.rmem_max = 8388608` | a **host** sysctl. Devices push syslog and flow over UDP; the kernel silently clamps the 8 MiB the collectors request to 208 KiB on stock Ubuntu and drops the rest with no error |

Full numbers: [docs/scale.md](docs/scale.md). Detection and manual fixes for each:
[docs/how-to/fix-silent-data-loss.md](docs/how-to/fix-silent-data-loss.md).

## Testing

Three layers, because "up" and "working" are different claims:

1. `test_containers.py` — containers running and healthy
2. `test_services.py` — each service's own readiness endpoint answers
3. `test_wiring.py` — components are actually connected to each other (Prometheus targets `up`,
   Grafana datasources healthy, Loki has a `device` label, MCP tools registered)

Layer 3 is the one that catches real problems; everything can be green at layers 1 and 2 while
nothing is connected.

**After touching anything TextFSM, run `make test-templates` first.** It needs no stack and no
devices and finishes in seconds.

## Device work — two engines, pyATS additive

| Layer | Tool | Job |
|---|---|---|
| transport | **Netmiko** | get text off the device, push config to it |
| tabular parsing | **TextFSM** + ntc-templates | `show` output → rows (`automation/textfsm/`) |
| config parsing | **TTP** | running-config → structure (`automation/ttp/`) |
| normalising | `automation/assurance/normalise.py` | three vendors' rows → one shape |
| structured state | **pyATS / Genie** | `learn()` / `parse()` where Genie supports it (`automation/pyats/`) |
| assurance | `automation/assurance/rules.yml` | rules with `source: interfaces` or `source: pyats` |
| comparison | **DeepDiff** | pre/post snapshots → what actually changed |

**What Genie actually supports — verified against the installed library, not the docs:**

| Platform | unicon | Genie parsers | Genie `learn()` | pyATS used for |
|---|---|---|---|---|
| Cisco IOS-XE | `iosxe` | hundreds | interface, platform, bgp, lldp, … | BGP (`learn`) |
| Huawei VRP | `hvrp` | **BGP only** — `display bgp peer` | **none** | BGP (`parse`) |
| MikroTik | — | — | — | nothing |

**Read that Huawei row carefully.** pyATS *connects* to Huawei and Genie *parses* its BGP output,
so "pyATS supports Huawei" is true — but there is no hvrp interface parser and no hvrp `learn()`
model of any kind. `learn("interface")` on a VRP box connects successfully and returns nothing.
This is why pyATS is additive: interface assurance stays on TextFSM for every platform.

What each platform gets is declared in `platforms.yml` under `pyats:` (`os`, `learn`, `parse`).
Two tests keep it honest: one against a hardcoded table of verified support, one that runs inside
the automation image and checks every declaration against the **installed** Genie.

A pyATS rule a platform cannot support returns **`skipped`** with the reason — never `pass`
(which would claim a check ran) and never `fail` (which would blame the device for Genie's gap).

## Gotchas that cost real time

| Area | Constraint |
|---|---|
| Infrahub schema | every `description:` must be **under 128 characters** — one longer field fails the entire schema load with `string_too_long` and never names the field |
| Infrahub schema | attribute and relationship **names need at least 3 characters** (`os`, `ip` fail the whole load with `string_too_short`) |
| Infrahub schema | reverse lists (a site's hosts, a host's services) are `kind: Component`: `seed.py` never writes them, so seeding a parent cannot unlink its children. Forward sides are `kind: Attribute` |
| Seeding | a new **kind** is the only code change: add it to `SECTIONS` in `seed.py`, after every kind it refers to |
| Infrahub compose | Postgres 18+ mounts at `/var/lib/postgresql`, **not** `/var/lib/postgresql/data` |
| Infrahub compose | Prefect is embedded in the Infrahub image — no separate Prefect image |
| Infrahub API | every attribute comes wrapped as `{"value": x}` — flatten before returning it to a model |
| Infrahub SDK | `prefetch_relationships=True` or `node.site.peer` raises `NodeNotFoundError` |
| Seeding | `seed.py` is schema-driven: never give it a field list, and an unknown key must fail — silently dropping it reports success while Infrahub never gets the value |
| Device address | `management_host` wins over `management_ip`; the renderer and Nornir must agree on which one they use |
| Nornir | omit `group_mappings` in the Infrahub inventory plugin — it resolves peers it never fetched, so `slugify()` raises `TypeError` before any host loads |
| Telegraf | `--watch-config poll`, never inotify — inotify is unreliable across a volume mount |
| Loki | labels are `device, site, role, severity` **only**. Message body and Cisco mnemonics stay fields — promoting a mnemonic to a label multiplies stream count by the number of message types. The mnemonic must stay in the stored log line, or it is not searchable at all |
| Prometheus | never let per-flow IPs or ports become labels; the flow config drops them on purpose. Flow labels are `device, site, role, protocol, direction, source, application, criticality` — all bounded |
| Grafana + Loki | every Loki target needs a matcher that cannot match empty (`device=~".+"`) besides the `$device` one — a variable expanding to `.*` otherwise fails the whole panel |
| prometheus_client | `expiration_interval` must exceed `SNMP_INTERVAL` plus a full retry round. At the 60 s default it equalled the poll interval and series flickered out between polls — counts dropped, "not reporting" flashed |
| Flow metrics | the metric is `netflow_flow_bytes_total` (not `flow_bytes_total`) and it is a **gauge** — bytes per 60 s window. Use `avg_over_time(...)/60` for bytes/s, never `rate()` |
| Telegraf conf.d | must never reference a file under `generated/`: conf.d reloads on `git pull`, before `make render`, and `processors.lookup` refuses to start on a missing file. Processors that need rendered tables are rendered too (`netflow-applications.conf`) |
| render-hook | the only thing that rewrites collector config without a human. Signed (HMAC), `expose:` only, refuses to run without `RENDER_HOOK_SECRET`. Off by default — profile `auto-render` |
| Flow behind NAT | every exporter arrives from the NAT address, so the source-IP lookup labels nothing. A device's `flow_port` renders its own listener tagged `flow_exporter`, which the lookup tries first. Ports must sit inside the compose-published `FLOW_DEDICATED_*` range or nothing arrives — the renderer refuses those |
| TextFSM | an empty parse must **raise** — `[]` and "device has nothing to report" are indistinguishable, so a missing template silently returns a wrong answer |
| Cisco syslog | IOS does **not** emit conformant RFC3164 (counter and hostname come before the timestamp). A strict parser drops every line silently |
| Log time | Loki files a line under the device's timestamp only when it names its zone and is near arrival; otherwise arrival time, tagged `clock_skew`. Never widen the window past Loki's out-of-order and future limits |
| SNMP security | set per device (`snmp_security`). Never lower the fleet's level to accommodate one device that cannot encrypt |
| SNMP timing | `SNMP_TIMEOUT x (SNMP_RETRIES + 1)` is what a dead device costs its shard — keep it ≤ 20 s (scale.md). Prefer more retries over a longer timeout: lost UDP replies are common, slow replies rare |
| MikroTik SNMP | no vendor CPU MIB — uses HOST-RESOURCES-MIB `hrProcessorLoad` |
| UDP buffers | the most consequential host setting. syslog and flow are UDP: an undersized `net.core.rmem_max` drops datagrams with **no error anywhere**, and the application's larger request is clamped without complaint. Only `netstat -su` shows it |
| install.py | stdlib only — it runs before pip has been used. A test asserts no third-party imports |
| Device sessions | the inventory is cached, so `.filter()` views share `Host` objects and therefore the SSH connection. Every entry point that touches a device takes `device_lock(name)` — without it two concurrent requests interleave on one session. Pass `nr=` through when adding an operation, or you add a login |
| Normalising | a normaliser reading a field the parser does not emit yields plausible but **wrong** booleans. `_require()` in `automation/assurance/normalise.py` exists because RouterOS flags live in `status`, not `flags` — reading the wrong key reported every running interface as down |
| Parsing split | **TextFSM** for tabular `show` output, **TTP** for hierarchical config. Do not point TextFSM at a config file; it has no notion of nested blocks |
| ntc-templates | IOS-XE templates are filed under `cisco_ios`, not `cisco_xe`. That is why `platforms.yml` carries both `netmiko_type` (how to connect) and `textfsm_platform` (how templates are named) |
| pyATS sessions | unicon opens its **own** SSH session, separate from Netmiko's. `run_assurance` holds `device_lock` across both so they never overlap on one device — which is why that lock is an `RLock`: the TextFSM read re-takes it from the same thread, and a plain `Lock` deadlocks |
| BGP sessions | identify by **(vrf, af, peer)**, never by peer address alone. Genie's own iosxe fixture has 2.2.2.2 in both VRF1 and default — separate sessions. Collapsing by IP lets one being down hide behind the other being up |
| Scheduled assurance | `automation/service/scheduler.py` runs inside the API process — keep uvicorn single-worker, or every worker assures every device. Off by default (`ASSURANCE_INTERVAL_MINUTES=0`): each run is an SSH session per device. Rule detail stays out of labels (`/assurance/latest`) so series stay devices × rules |
| Network Map (Weathermap NG) | the dashboard is GENERATED by `render-network-map.py` from `source-of-truth/map/*.yml` — never hand-edit `provisioning/dashboards/generated/`. A link half shows the series whose display name equals its `query`, so the target legends (`<device> <ifName> out|in|speed`, `STATUS <device>`) are a contract; `test_every_side_query_matches_a_target_legend` guards it. The panel fits the drawing to its width: only `map.scale.y` spreads nodes apart; scaling x shrinks everything |
| LLDP neighbours | a re-learned neighbour gets a new `remIndex`; count neighbours by name, not entries, or the port looks like a shared segment and the link vanishes. IOS has no LLDP on tunnels — tunnel links come from BGP |
| Single-file bind mounts | `platforms.yml` is mounted as a file. `git pull` replaces it (new inode) and the running container keeps the old content — `docker compose restart infrahub-server` (and `automation`) after a pull that changes it, or `make render` renders the old platforms |
| Genie fixtures | `automation/pyats/samples/*.json` are Genie's **own** golden test data, copied out of the installed package. Use them to test pyATS logic offline; don't hand-write Genie output |
| MCP Host allow-list | the MCP SDK answers only `Host` headers on a server's allow-list and returns **421** for anything else — while `/healthz` (outside the MCP app) stays green. `FastMCP(name)` defaults to a loopback-only list; `common.build()` sets loopback + `mcp-<name>:*` + `MCP_ALLOWED_HOSTS`. `mcp` is pinned (`==1.30.*`) because a minor bump turned this on silently. Verify with `make mcp-check`, never `/healthz` |
| MCP caller identity | the auth middleware puts the caller on the request scope; a tool reads it via its `Context` (`common.caller(ctx)` / `require_role(ctx)`). A ContextVar does **not** reach the tool — in a stateful session tools run in the session task, not the request task |
| MCP sync tools | FastMCP runs a sync tool on the event loop; `common._Server` wraps every sync tool in a worker thread. Keep tools sync and let it — a slow device call otherwise stalls the whole server |
| Graphs in chat | `render_interface_graph` returns a Markdown image LINK, never image bytes: chat platforms drop non-text tool content, and a PNG in model context costs thousands of tokens. The PNG lives in `mcp/images.py` (in memory, TTL, bounded); `/g/<id>.png` is a public route — the random id is the authorisation. It renders panels 1/2 of `darqcube-interface` by id; `test_graph_panels_keep_their_ids` guards that. A registered tool is replaced by its async wrapper, so tools must not call each other — share plain helpers (`_dashboard_url`) |
| ai-platform overlay | `sites/ai-platform.yml` is generated on the ai-platform host (`scripts/ai-platform-connect.py`) and may contain **only** `ai_platform` — `merge_overlay` refuses anything else. Without a site file, `install.py` changes only the MCP keys in the existing `.env` (`set_env_values` appends keys an older `.env` lacks) |

## Don't

- Add a **passthrough MCP tool** — no raw PromQL, LogQL, GraphQL, or `run_command(device, anything)`.
  Every tool builds its query server-side from bounded arguments. One passthrough tool makes every
  other boundary in the stack decorative.
- **Publish MCP ports beyond `MCP_BIND_IP`.** They bind `127.0.0.1` by default; another host is
  opened only through `ai_platform.publish` (which also sets `MCP_ALLOWED_HOSTS`). Never bind
  `0.0.0.0` or add `ports:` in a tracked file.
- **Name a specific ai-platform product** in code, docs or tests — say "ai-platform". Project-specific
  values (such as the header an ai-platform forwards tokens in) are passed as arguments, never
  hardcoded. `test_neutral.py` enforces this and the no-lab-addresses rule.
- Register `push_device_config` when `MCP_ALLOW_WRITE` is false. It must be absent from `tools/list`,
  not merely refuse when called. Note the flag gates the MCP **tool** only — the automation API
  beneath has no auth, so it is not a system-wide write switch. Don't describe it as one. In
  `oidc` mode it also calls `require_role(ctx)` before pushing — keep that first.
- Add a component that isn't in one of the four groups without saying why.
