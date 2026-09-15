# Prometheus and Loki — storage

## Prometheus

Stores every metric. Scrapes Telegraf plus the stack's own services; evaluates
alert and recording rules.

Without it: metrics, alerts and metric dashboards stop. Logs and automation are
unaffected.

### Configuration

| Where | What |
|---|---|
| `observability/prometheus/prometheus.yml` | scrape targets and intervals |
| `observability/prometheus/rules/alerts.yml` | alert rules |
| `observability/prometheus/rules/recording.yml` | derived metrics |
| `.env` → `PROM_RETENTION` | how long metrics are kept (default 15d) |
| `.env` → `PROMETHEUS_PORT` | published port (default 19090) |

### Recording rules matter here

`memory_used_percent` is *derived*, because the three vendors report memory in
percent, bytes and allocation units respectively. The `memory_kind` tag says
which, and the rule converts. **Alert on `device:memory_used_percent:max`, not
on `memory_used`.**

`device:cpu_usage:max` similarly collapses the per-core series into one value
per device.

### Reload without restarting

```bash
curl -X POST localhost:${PROMETHEUS_PORT}/-/reload
```

`--web.enable-lifecycle` is on.

### Verify

```bash
source .env
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/targets" \
  | .venv/bin/python -c "import json,sys;[print(t['labels']['job'],t['health']) for t in json.load(sys.stdin)['data']['activeTargets']]"
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=up"
```

### Problems

| Symptom | Cause |
|---|---|
| A `${VAR}` appears as a literal label | **Prometheus does not expand environment variables in its config**, and promtool does not warn. Use `config-init` for anything that must vary. |
| Rules do not load | `promtool check rules observability/prometheus/rules/*.yml` |
| Duplicate-rule lint error | two rules recording the same metric name — combine them with `or` |
| Disk filling | lower `PROM_RETENTION` or raise `SNMP_INTERVAL` |

---

## Loki

Stores every log line, labelled from the source of truth by Logstash.

Without it: log search and log panels stop. Metrics and automation are
unaffected.

### Configuration

| Where | What |
|---|---|
| `observability/loki/loki.yml` | storage, limits, retention |
| `.env` → `LOKI_RETENTION` | default 336h (14 days) |
| `.env` → `LOKI_PORT` | published port (default 13100) |

### Label cardinality

Labels are `device`, `site`, `role`, `severity`, `platform` — and nothing else.
Loki creates a stream per unique label combination, so adding a per-message
field (a Cisco mnemonic, a RouterOS topic) multiplies stream count by the number
of distinct message types.

`max_label_names_per_series: 12` in `loki.yml` is a backstop: if a future edit
starts promoting message fields to labels, ingestion fails loudly instead of
quietly filling the index.

The message body stays searchable as content:

```logql
{device="cr1"} |= "LINK_STATE"
{site="hq", severity="error"}
```

### Verify

```bash
source .env
curl -s "localhost:${LOKI_PORT}/ready"
curl -s "localhost:${LOKI_PORT}/loki/api/v1/labels"           # expect device, site, role
curl -sG "localhost:${LOKI_PORT}/loki/api/v1/query_range" --data-urlencode 'query={site="hq"}'
```

`device` appearing in the label list proves the whole syslog path works — the
device sent it, Logstash parsed it, and it matched the Infrahub identity table.

### Problems

| Symptom | Cause |
|---|---|
| Will not start: `not a valid duration string "${LOKI_RETENTION}"` | needs `-config.expand-env=true`, which the compose file sets |
| `field kind not found in type ring.RingConfig` | ring config must be `kvstore.store`, not `kind` |
| `device` label missing | Logstash is not enriching — see [05-logstash.md](05-logstash.md) |
| Ingestion rejected | too many labels — check what the pipeline is promoting |
