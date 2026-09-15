# Change retention, intervals and limits

Almost everything is in `.env`. Anything not in `.env` is in the component's own
config file, listed below.

## In `.env`

| Variable | Default | Effect |
|---|---|---|
| `PROM_RETENTION` | `15d` | how long metrics are kept |
| `LOKI_RETENTION` | `336h` | how long logs are kept (14 days) |
| `SNMP_INTERVAL` | `30s` | how often devices are polled |
| `AUTOMATION_CONCURRENCY` | `8` | devices talked to at once |
| `MAX_CONFIG_LINES` | `200` | cap on a single config push |
| `INFRAHUB_BRANCH` | `main` | which Infrahub branch is read |
| `COMPOSE_PROFILES` | `devices,automation,mcp` | which groups start |

Apply with `docker compose up -d` — only changed services are recreated.

## Polling interval

`SNMP_INTERVAL=30s` is a demo value. On a real network with hundreds of devices
use `60s` or more: every poll opens an SNMP session per device, and interface
tables are large.

It must stay **shorter** than Prometheus's `scrape_interval` (30s in
`prometheus.yml`), or samples are missed.

## Retention and disk

Rough sizing: Prometheus uses ~1–2 bytes per sample after compression. 50
devices × 30 interfaces × 8 metrics × (86400/30) samples/day ≈ 1.5 GB/day at 30s
polling. `15d` retention is therefore ~20 GB. Raise `SNMP_INTERVAL` before
raising the disk.

Loki depends entirely on how chatty the devices are; the `336h` default is
conservative.

## Running only part of the stack

```bash
COMPOSE_PROFILES=                        # Infrahub + observability only
COMPOSE_PROFILES=devices                 # + collection
COMPOSE_PROFILES=devices,automation      # + get/put on devices
COMPOSE_PROFILES=devices,automation,mcp  # + the AI tool surface
```

Dropping `mcp` removes no functionality from anything else — nothing in the
stack depends on those servers.

## Resource limits

Not set by default, deliberately: this is a demo stack and a container hitting
an invisible cap is a confusing failure. To add one:

```yaml
  prometheus:
    deploy:
      resources:
        limits: { cpus: "2", memory: 4g }
```

Neo4j is the memory-hungriest service; its heap is set in
`compose/source-of-truth.yaml` (`NEO4J_server_memory_heap_max__size: 2G`).

## Config files, for what `.env` does not cover

| File | Holds |
|---|---|
| `observability/prometheus/prometheus.yml` | scrape targets and intervals |
| `observability/prometheus/rules/alerts.yml` | alert rules |
| `observability/prometheus/rules/recording.yml` | derived metrics |
| `observability/loki/loki.yml` | ingestion limits, label caps |
| `observability/alertmanager/alertmanager.yml.tmpl` | routing, grouping, inhibition |
| `observability/telegraf/conf.d/*.conf` | shared inputs, processors, output |
| `observability/telegraf/profiles/*.tmpl` | what SNMP collects |

**Prometheus does not expand environment variables in its config file.** A
`${VAR}` there is stored literally as a label value and `promtool` will not
complain. Anything that must vary per install goes in `.env` and is substituted
by `config-init` — that is why `alertmanager.yml` is a `.tmpl`.

## After changing a config file

```bash
# Prometheus — reload without restart
curl -X POST localhost:${PROMETHEUS_PORT:-9090}/-/reload

# Telegraf — picks up generated/ within 30s on its own; restart for conf.d/
docker compose restart telegraf

# Logstash, Loki, Alertmanager
docker compose restart logstash loki alertmanager
```
