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
      AU["Nornir · Netmiko<br/>TextFSM · TTP<br/>:8100"]
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
| Config & state | SSH / TCP 22 | stack pulls + puts | **Nornir · Netmiko · TextFSM · TTP** | files + API |

Telegraf never listens for syslog; Logstash never polls a device.

## Two deliberate limits

**Flow data is aggregated at the collector.** Per-flow source and destination
addresses and ports are dropped before they reach Prometheus, because as labels
they are unbounded cardinality — a busy link would produce hundreds of thousands
of series within hours. The stack answers "how much traffic, of what kind", not
"who is talking to whom". The latter needs a flow store, which this stack does
not run.

**Assurance is vendor-neutral, and that shaped the tooling.** There is one path
for all three platforms: Netmiko gets the text, TextFSM parses tabular `show`
output, TTP parses hierarchical config, and `automation/assurance/normalise.py`
flattens the vendor differences into one shape.

A single `rules.yml` then covers the fleet, and adding a check is a YAML edit
rather than a code change. The cost is one normaliser function per platform —
about fifteen lines — which is what a second vendor-specific assurance path
would have cost many times over.
