# Add or change an alert

Alerts live in `observability/prometheus/rules/alerts.yml`. It is plain
Prometheus rule YAML — no templating, no generation.

## Add one

```yaml
groups:
  - name: network-devices          # add to an existing group, or make a new one
    rules:
      - alert: InterfaceFlapping
        expr: changes(interface_oper_status[15m]) > 6
        for: 5m                     # must be true this long before firing
        labels:
          severity: warning         # warning | critical
        annotations:
          summary: "{{ $labels.device }} {{ $labels.ifName }} is flapping"
          description: "{{ $value }} state changes in 15 minutes"
```

Check and apply:

```bash
docker run --rm -v "$PWD/observability/prometheus:/p:ro" --entrypoint promtool \
  prom/prometheus:v3.4.1 check rules /p/rules/alerts.yml

curl -X POST localhost:${PROMETHEUS_PORT:-9090}/-/reload
```

`--web.enable-lifecycle` is on, so a reload needs no restart.

## The metrics you can alert on

| Metric | Meaning |
|---|---|
| `interface_oper_status` | 1 = up, 2 = down (IF-MIB) |
| `interface_admin_status` | 1 = up, 2 = shut |
| `interface_in_octets` / `_out_octets` | 64-bit counters — use `rate()` |
| `interface_in_errors` / `_out_errors` | error counters |
| `interface_speed` | Mbit/s |
| `cpu_usage` | percent, one series per core/entity |
| `memory_used` / `_free` / `_total` | raw, units vary — see below |
| `device_uptime` | SNMP sysUpTime |
| `flow_bytes_total` / `flow_packets_total` | NetFlow/IPFIX, by protocol |
| `device:cpu_usage:max` | recording rule — one value per device |
| `device:memory_used_percent:max` | recording rule — comparable across vendors |

Every metric carries `device`, `site`, `role` and `platform` from Infrahub, plus
`ifName`, `ifAlias` and `ifType` on interface metrics.

**Alert on `device:memory_used_percent:max`, not on `memory_used`.** The three
vendors report memory in percent, bytes and allocation units respectively; the
recording rule in `observability/prometheus/rules/recording.yml` is what makes them comparable.

## Useful patterns

```promql
# Admin-up but oper-down — ignores deliberately shut ports
interface_oper_status == 2 and interface_admin_status == 1

# Physical ports only (ifType 6), excluding tunnels/SVIs/loopbacks
interface_oper_status{ifType="6"} == 2

# A device that was being polled and has stopped
absent_over_time(device_uptime{device="cr1"}[10m])

# Only alert on core devices
device:cpu_usage:max{role="core"} > 85
```

## Sending alerts somewhere

Alertmanager delivers to one webhook, set in `.env`:

```bash
ALERT_WEBHOOK_URL=https://your-ai-platform.example/alerts
```

```bash
docker compose up -d --force-recreate config-init alertmanager
```

Left empty, alerts collect in the Alertmanager UI and go nowhere — the stack
starts with no external dependency.

**Alertmanager has no environment-variable expansion of its own**, so
`observability/alertmanager/alertmanager.yml.tmpl` is a template and the
`config-init` service substitutes the URL into a volume at startup. Edit the
`.tmpl`, never the rendered copy.

For Slack, email or PagerDuty, add a receiver to the template:

```yaml
receivers:
  - name: default
    webhook_configs:
      - url: __ALERT_WEBHOOK_URL__
    slack_configs:
      - api_url: https://hooks.slack.com/services/XXX
        channel: "#network"
```

## Silencing

Use the Alertmanager UI at `localhost:${ALERTMANAGER_PORT}` → Silences, or set a
device's status to `maintenance` in `devices.yml` and re-run `make seed &&
make render` to stop polling it entirely.
