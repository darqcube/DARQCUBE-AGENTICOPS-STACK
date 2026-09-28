# Architecture

## Infrastructure

One VM, four container groups, three device platforms. Dashed lines are
stack-initiated (the stack reaches out); solid lines are device-initiated (the
device pushes to us). That distinction decides your firewall rules.

```mermaid
flowchart LR
  subgraph DEV["Network Devices"]
    direction TB
    C["Cisco IOS-XE"]
    H["Huawei VRP"]
    M["MikroTik RouterOS"]
  end

  subgraph VM["Single VM — Docker Compose"]
    direction TB
    subgraph SOT["📋 source-of-truth"]
      direction LR
      IH["Infrahub :8000"]
      NEO[("Neo4j")]
      SUP["redis · rabbitmq<br/>postgres"]
    end
    subgraph OBS["📊 observability"]
      direction LR
      TG["Telegraf"]
      LS["Logstash"]
      PR[("Prometheus :9090")]
      LK[("Loki :3100")]
      AM["Alertmanager :9093"]
      GF["Grafana :3000"]
    end
    subgraph AUT["⚙️ automation"]
      AU["Nornir · Netmiko<br/>TextFSM · TTP · pyATS<br/>:8100"]
    end
    subgraph MCPG["🔌 mcp"]
      MS["6 servers<br/>internal only"]
    end
  end

  AIP["🤖 AI-Platform<br/>(optional)"]

  DEV -. "SNMP 161 · gNMI 57400" .-> TG
  DEV == "NetFlow 2055 · IPFIX 4739" ==> TG
  DEV == "syslog 514" ==> LS
  AU -. "SSH 22 — get / put config" .-> DEV

  IH -. "device · site · role" .-> OBS
  IH -. "inventory" .-> AU
  IH --- NEO
  IH --- SUP
  TG --> PR --> AM
  LS --> LK
  PR --> GF
  LK --> GF
  IH --- MS
  OBS --- MS
  AU --- MS
  MS -. optional .-> AIP
  AM -. "webhook, optional" .-> AIP

  %% Colour follows the four container groups — the same grouping as the
  %% com.darqcube.group label. Light fills with explicit dark text, so the
  %% diagram stays readable in GitHub's light AND dark themes.
  classDef device  fill:#dbeafe,stroke:#1d4ed8,stroke-width:2px,color:#1e293b
  classDef sot     fill:#fef3c7,stroke:#b45309,stroke-width:2px,color:#1e293b
  classDef backing fill:#fefce8,stroke:#a16207,stroke-width:1px,color:#1e293b
  classDef collect fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#1e293b
  classDef store   fill:#cffafe,stroke:#0e7490,stroke-width:2px,color:#1e293b
  classDef alert   fill:#fee2e2,stroke:#b91c1c,stroke-width:2px,color:#1e293b
  classDef ui      fill:#f3e8ff,stroke:#7e22ce,stroke-width:2px,color:#1e293b
  classDef auto    fill:#ffedd5,stroke:#c2410c,stroke-width:2px,color:#1e293b
  classDef mcp     fill:#ccfbf1,stroke:#0f766e,stroke-width:2px,color:#1e293b
  classDef ai      fill:#e2e8f0,stroke:#475569,stroke-width:2px,stroke-dasharray:5 3,color:#1e293b

  class C,H,M device
  class IH sot
  class NEO,SUP backing
  class TG,LS collect
  class PR,LK store
  class AM alert
  class GF ui
  class AU auto
  class MS mcp
  class AIP ai

  style DEV  fill:#f8fafc,stroke:#1d4ed8,stroke-width:2px,color:#1e293b
  style VM   fill:#ffffff,stroke:#94a3b8,stroke-width:2px,color:#1e293b
  style SOT  fill:#fffbeb,stroke:#b45309,color:#1e293b
  style OBS  fill:#f0fdf4,stroke:#15803d,color:#1e293b
  style AUT  fill:#fff7ed,stroke:#c2410c,color:#1e293b
  style MCPG fill:#f0fdfa,stroke:#0f766e,color:#1e293b

  %% Thick = the device pushes to us. Dotted = we reach out to the device.
  linkStyle 0 stroke:#1d4ed8,stroke-width:2px
  linkStyle 1,2 stroke:#15803d,stroke-width:3px
  linkStyle 3 stroke:#c2410c,stroke-width:2px
  linkStyle 4,5 stroke:#b45309,stroke-width:2px
  linkStyle 6,7 stroke:#a16207,stroke-width:1px
  linkStyle 8,9,10,11,12 stroke:#0e7490,stroke-width:2px
  linkStyle 13,14,15 stroke:#0f766e,stroke-width:1px
  linkStyle 16,17 stroke:#475569,stroke-width:1px
```

Ports shown are the standard values. The published host ports are configurable —
see [how-to/change-ports.md](how-to/change-ports.md).

## Component interdependency

A different question: **what breaks when something is down.** Arrows point from
a component to the things that depend on it.

```mermaid
flowchart TD
  IH["📋 Infrahub<br/><i>source of truth</i>"]
  RI["render-inventory<br/><i>make render</i>"]
  TG["Telegraf"]
  LS["Logstash"]
  PR["Prometheus"]
  LK["Loki"]
  AM["Alertmanager"]
  GF["Grafana"]
  AU["Automation API"]
  MS["🔌 MCP servers"]

  IH --> RI --> TG & LS
  IH --> AU
  TG --> PR --> AM
  LS --> LK
  PR & LK --> GF
  IH & PR & LK & GF & AU --> MS

  %% Red = nothing works without it. Amber = a whole capability stops.
  %% Green = degraded but the stack keeps running. Grey = optional.
  classDef critical fill:#fee2e2,stroke:#b91c1c,stroke-width:3px,color:#1e293b
  classDef major    fill:#fef3c7,stroke:#b45309,stroke-width:2px,color:#1e293b
  classDef degraded fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#1e293b
  classDef optional fill:#e2e8f0,stroke:#475569,stroke-width:2px,stroke-dasharray:5 3,color:#1e293b

  class IH critical
  class PR,LK major
  class TG,LS,RI,AU degraded
  class GF,AM,MS optional
```

**Red** — nothing else can be added or changed without it.
**Amber** — a whole class of data stops.
**Green** — one capability stops, the rest keeps running.
**Grey** — optional; losing it costs an interface, not data.

| If this is down | Still works | Stops working |
|---|---|---|
| **Infrahub** | all collection, dashboards, alerts | adding/changing devices; `make render`; automation (live inventory) |
| **render-inventory** | everything already collecting | new devices reaching the collectors |
| **Telegraf** | syslog → Loki; automation | all metrics and flows |
| **Logstash** | metrics; automation | all log ingest |
| **Prometheus** | logs; automation | metrics, alerts, metric dashboards |
| **Loki** | metrics; automation | log search, log panels |
| **Alertmanager** | everything; alerts still evaluate | alert delivery |
| **Grafana** | collection continues; alerts still fire | the UI only |
| **Automation API** | all observability | get/put config — and mcp-netmiko / mcp-assurance with it |
| **MCP servers** | **everything** | the AI-Platform interface only |

The last row is the important one. MCP sits at the bottom of the graph with
nothing depending on it, which is what makes "works with or without AI" a
structural property rather than a claim. Verify it:

```bash
COMPOSE_PROFILES=devices,automation make up
make test                                   # still green
```

## Why Infrahub sits above everything

Every collector is labelled from it. A metric from an SNMP poll and a log line
from syslog both arrive carrying the same `device`, `site` and `role`, because
`make render` writes one identity table that Telegraf and Logstash both read.

That is the whole reason a Grafana panel, a PromQL alert and a LogQL query line
up on the same device. Without it you have three tools that each know a device
by a different name.

## Ingest ownership

Each feed has exactly one owner. No feed is collected twice.

| Feed | Protocol | Who starts it | Collector | Store |
|---|---|---|---|---|
| Metrics | SNMPv3 / UDP 161 | stack pulls | **Telegraf** | Prometheus |
| Streaming telemetry | gNMI / TCP 57400 | stack pulls | **Telegraf** | Prometheus |
| Flow records | NetFlow, IPFIX / UDP | device pushes | **Telegraf** | Prometheus |
| Events | Syslog / UDP 514 | device pushes | **Logstash** | Loki |
| Config & state | SSH / TCP 22 | stack pulls + puts | **Nornir · Netmiko · TextFSM · TTP · pyATS** | files + API |

Telegraf never listens for syslog; Logstash never polls a device.

## Volumes and storage

One rule decides where everything lives: **configuration comes from the
repository through bind mounts; state lives in Docker volumes.** The repo is
what you version and edit. The volumes are what the running stack accumulates.

```mermaid
flowchart LR
  subgraph REPO["Repository · configuration you edit · read-only"]
    direction TB
    CFG_SOT["source-of-truth/<br/>schema · devices · scripts"]
    PLAT["platforms.yml"]
    CFG_OBS["observability/<br/>prometheus · loki · grafana<br/>logstash · telegraf · alertmanager"]
  end

  subgraph SOTC["Containers · source of truth + automation"]
    direction TB
    BACK["neo4j · redis · rabbitmq<br/>task-db · task-manager"]
    IH["infrahub-server<br/>infrahub-worker"]
    AU["automation"]
  end

  subgraph WR["Written into the repo · emptied by make clean"]
    direction TB
    GEN["observability/telegraf/generated/<br/>SNMP shards · devices.json<br/>devices.yml"]
    CONFS["automation/configs/<br/>fetched running configs"]
  end

  subgraph OBSC["Containers · observability"]
    direction TB
    TG["telegraf"]
    LS["logstash"]
    OBS["prometheus · loki<br/>grafana · alertmanager"]
  end

  subgraph VOL["Docker volumes · deleted by make clean"]
    direction TB
    V_SOT[("neo4j-data · task-db-data<br/>infrahub-storage<br/>redis-data · rabbitmq-data")]
    V_OBS[("prometheus-data · loki-data<br/>grafana-data · alertmanager-*")]
  end

  CFG_SOT -.-> IH
  PLAT -.-> IH
  PLAT -.-> AU
  CFG_OBS -.-> TG
  CFG_OBS -.-> LS
  CFG_OBS -.-> OBS
  IH == "make render" ==> GEN
  AU == "config-get" ==> CONFS
  GEN -.-> TG
  GEN -.-> LS
  IH --- V_SOT
  BACK --- V_SOT
  OBS --- V_OBS

  %% Grey = configuration you edit. Amber = folders a container writes back
  %% into the repo — make clean empties them. Cyan = Docker-managed state.
  %% Containers keep their group colours from the diagrams above.
  classDef cfg     fill:#f1f5f9,stroke:#475569,stroke-width:1px,color:#1e293b
  classDef written fill:#fef3c7,stroke:#b45309,stroke-width:2px,stroke-dasharray:5 3,color:#1e293b
  classDef vol     fill:#cffafe,stroke:#0e7490,stroke-width:2px,color:#1e293b
  classDef sot     fill:#fef3c7,stroke:#b45309,stroke-width:2px,color:#1e293b
  classDef collect fill:#dcfce7,stroke:#15803d,stroke-width:2px,color:#1e293b
  classDef store   fill:#cffafe,stroke:#0e7490,stroke-width:2px,color:#1e293b
  classDef auto    fill:#ffedd5,stroke:#c2410c,stroke-width:2px,color:#1e293b

  class CFG_SOT,PLAT,CFG_OBS cfg
  class GEN,CONFS written
  class V_SOT,V_OBS vol
  class IH,BACK sot
  class TG,LS collect
  class OBS store
  class AU auto

  style REPO fill:#ffffff,stroke:#475569,stroke-width:2px,color:#1e293b
  style SOTC fill:#ffffff,stroke:#94a3b8,stroke-width:2px,color:#1e293b
  style OBSC fill:#ffffff,stroke:#94a3b8,stroke-width:2px,color:#1e293b
  style WR   fill:#fffbeb,stroke:#b45309,stroke-width:2px,color:#1e293b
  style VOL  fill:#ecfeff,stroke:#0e7490,stroke-width:2px,color:#1e293b

  %% 0-5 read-only mounts · 6-7 writes · 8-9 generated read back · 10-12 volumes
  linkStyle 0,1,2,3,4,5 stroke:#1d4ed8,stroke-width:1.5px
  linkStyle 6,7 stroke:#c2410c,stroke-width:3px
  linkStyle 8,9 stroke:#b45309,stroke-width:1.5px
  linkStyle 10,11,12 stroke:#0e7490,stroke-width:2px
```

**Grey** — configuration you edit, mounted read-only (dotted blue).
**Amber, dashed** — folders a container writes back into the repo (thick
orange). Gitignored, emptied by `make clean`, and `generated/` is read back by
Telegraf and Logstash (dotted amber).
**Cyan** — Docker-managed state. `make clean` deletes it.

### Named volumes — the state

Docker-managed, stored under `/var/lib/docker/volumes/`. Invisible to `git`,
and deleted by `make clean`.

| Volume | Group | Used by | Holds | If lost |
|---|---|---|---|---|
| `neo4j-data` | source-of-truth | `neo4j` | the Infrahub graph — every device, site and role | see below |
| `neo4j-logs` | source-of-truth | `neo4j` | Neo4j logs | nothing of value |
| `task-db-data` | source-of-truth | `task-db` | Postgres for Infrahub's embedded Prefect | Infrahub task history |
| `task-manager-data` | source-of-truth | `task-manager` | Prefect home | nothing of value |
| `infrahub-storage` | source-of-truth | `infrahub-server`, `infrahub-worker` | Infrahub artifacts, shared by both | Infrahub-side files |
| `redis-data` | source-of-truth | `redis` | Infrahub's cache | nothing — rebuilt on demand |
| `rabbitmq-data` | source-of-truth | `rabbitmq` | Infrahub's message queue | nothing — transient |
| `prometheus-data` | observability | `prometheus` | every metric | metric history (~4 GB per 15 days at 400 devices) |
| `loki-data` | observability | `loki` | every log line | log history |
| `grafana-data` | observability | `grafana` | users, sessions, preferences | little — dashboards come from the repo |
| `alertmanager-data` | observability | `alertmanager` | silences and notification state | active silences |
| `alertmanager-config` | observability | `config-init` writes, `alertmanager` reads | the rendered `alertmanager.yml` | nothing — regenerated at every start |

**`neo4j-data` is only irreplaceable if Infrahub was edited through its UI.**
Everything that arrived through `source-of-truth/devices/*.yml` rebuilds with
`make schema && make seed`. That is the strongest argument for making every
device change through the YAML: the repo, not the volume, stays the source of
truth for the source of truth.

**What to back up**, in order: `neo4j-data` and `task-db-data` if Infrahub has
been edited directly; `prometheus-data` and `loki-data` if history matters.
Everything else regenerates.

### Bind mounts — the configuration

Folders in the repository, mounted into containers. `make clean` does **not**
touch the read-only ones; it empties the two written ones.

**Read-only** — containers consume these and never write them. Edit on the
host, then restart or reload the service.

| Host path | Mounted into | As |
|---|---|---|
| `platforms.yml` | `infrahub-server`, `automation` | the vendor matrix |
| `source-of-truth/schema/` | `infrahub-server` | `/schema` |
| `source-of-truth/devices/` | `infrahub-server` | `/devices` |
| `source-of-truth/scripts/` | `infrahub-server` | `/scripts` — seed and render run here |
| `observability/prometheus/` | `prometheus` | config and rules |
| `observability/loki/` | `loki` | `loki.yml` |
| `observability/grafana/provisioning/` | `grafana` | datasources and dashboards |
| `observability/alertmanager/alertmanager.yml.tmpl` | `config-init` | the template it renders from |
| `observability/logstash/pipeline/` | `logstash` | the syslog pipeline |
| `observability/logstash/patterns/` | `logstash` | vendor grok patterns |
| `observability/telegraf/telegraf.conf` | `telegraf` | agent settings |
| `observability/telegraf/conf.d/` | `telegraf` | netflow, gNMI, outputs |

**Written by a container** — the two exceptions, where a container writes back
into the repo tree. Both are gitignored apart from a `.gitkeep`.

| Host path | Written by | Read by | Holds |
|---|---|---|---|
| `observability/telegraf/generated/` | `infrahub-server` (`make render`) | `telegraf`, `logstash` | SNMP and gNMI shards, `devices.json`, `devices.yml` |
| `automation/configs/` | `automation` | — | running configs fetched from devices |

`generated/` is a bind mount rather than a volume on purpose: it is where every
doc, `verify.sh` and the wiring test look for rendered output, and you can read
it on the host to see exactly what Telegraf and Logstash were given. The
renderer runs as root and Logstash reads as uid 1000, so the renderer sets its
files to `0644` explicitly rather than trusting the container's umask.

`automation/` is mounted as a whole so templates and tasks can be edited on the
host and retried without rebuilding the image. The container itself only ever
writes `automation/configs/`.

### What `make clean` removes — and what it does not

`make clean` asks first, then does two things:

1. `docker compose down -v` — deletes **every named volume** above.
2. Deletes every file except `.gitkeep` in the two **written** bind mounts,
   `observability/telegraf/generated/` and `automation/configs/`.

So after a clean:

- **All state is gone**: metrics, logs, the Infrahub graph, Grafana users.
- **Rendered config and fetched device configs are gone.**
- **All configuration you edit is untouched** — it is the repo. So are
  `site.yml` and `.env`.

Step 2 exists because `down -v` only removes *named* volumes; a bind mount is
a host folder and Docker leaves it alone. Without it, a stale `generated/`
kept Telegraf polling the previous install's devices until the next
`make render` — and could make the wiring test and `verify.sh` pass for an
install that had rendered nothing. And fetched running configs, which contain
SNMP communities and local password hashes, outlived the install.

If `docker compose down -v` fails, `make clean` stops and deletes nothing
else. The files are owned by root, but deleting them needs only write access
to the folder, which is yours — no `sudo`.

### Where it physically lives

```bash
docker volume ls --filter label=com.docker.compose.project=darqcube
docker system df -v | grep darqcube_          # size of each volume
```

Named volumes are prefixed with the project name — `darqcube_prometheus-data`
and so on — because `compose.yaml` sets `name: darqcube`.

## Two deliberate limits

**Flow data is aggregated at the collector.** Per-flow source and destination
addresses and ports are dropped before they reach Prometheus, because as labels
they are unbounded cardinality — a busy link would produce hundreds of thousands
of series within hours. The stack answers "how much traffic, of what kind", not
"who is talking to whom". The latter needs a flow store, which this stack does
not run.

**Assurance has two engines, and pyATS is additive.** TextFSM covers interface
state on every platform: `automation/assurance/normalise.py` flattens three
vendors' CLI formats into one shape, so a single rule set covers the fleet.

pyATS/Genie runs on top wherever Genie genuinely returns data — Cisco fully,
Huawei for BGP only, MikroTik not at all. It is never a platform's only path,
so a gap in Genie's coverage cannot silently remove a platform's checks; an
unsupported rule reports `skipped` rather than `pass`. What each platform gets
is declared in `platforms.yml` and verified against the installed Genie by a
test that runs inside the automation image.
