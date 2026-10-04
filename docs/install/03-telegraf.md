# Telegraf — SNMP, gNMI and flow

## What it does here

Owns every metric feed: SNMP polling, gNMI streaming, and NetFlow/IPFIX
listening. It attaches `device`, `site` and `role` from the Infrahub-rendered
identity table before anything reaches Prometheus.

It does **not** handle syslog — that is Logstash.

Without it: all metrics and flow stop. Logs and automation are unaffected.

## Configuration

| Where | What |
|---|---|
| `observability/telegraf/telegraf.conf` | agent settings |
| `observability/telegraf/conf.d/outputs.conf` | identity lookup, Prometheus output |
| `observability/telegraf/conf.d/netflow.conf` | flow listeners and aggregation |
| `observability/telegraf/conf.d/gnmi-normalise.conf` | maps gNMI strings to SNMP integers |
| `observability/telegraf/profiles/_interfaces.conf.tmpl` | IF-MIB, shared by all platforms |
| `observability/telegraf/profiles/_resources.conf.tmpl` | CPU/memory shape |
| `platforms.yml` | the per-vendor CPU and memory OIDs |
| `observability/telegraf/generated/` | **written by `make render` — never edit** (includes `netflow-dedicated.conf` when any device has a `flow_port`) |
| `.env` → `SNMP_INTERVAL`, `SNMPV3_*`, `NETFLOW_PORT`, `IPFIX_PORT`, `FLOW_DEDICATED_FIRST`, `FLOW_DEDICATED_LAST` | |

## Two SNMP inputs per device, on purpose

IF-MIB is byte-identical on Cisco, Huawei and MikroTik, so interfaces are
collected once for the **whole fleet** in one input. Only CPU and memory differ
per vendor, and those get one input per platform. That is why a whole platform
definition in `platforms.yml` is ~15 lines rather than 200.

## SNMPv3 security, per device

Every input is authPriv (SHA + AES) by default. Telegraf takes one security
level per input, so a device whose image cannot encrypt SNMP — Cisco L2 IOL is
one — is set to `snmp_security: auth_no_priv` in Infrahub, and the renderer
polls it from separate `snmp-*-authnopriv.conf` inputs. The rest of the fleet
stays authPriv; never lower the whole network for one device.

A device polled at a level it is not configured for drops the request
silently. Telegraf sees only a timeout.

## Metric names

`<measurement>_<field>`. The measurement name is half the metric name:

| Measurement | Field | Metric |
|---|---|---|
| `interface` | `oper_status` | `interface_oper_status` |
| `cpu` | `usage` | `cpu_usage` |
| `netflow` | `flow_bytes_total` | `netflow_flow_bytes_total` — a gauge: bytes per 60 s window |
| `memory` | `used` | `memory_used` |
| `device` | `uptime` | `device_uptime` |

## How to change it

Add a metric → [../how-to/add-a-metric.md](../how-to/add-a-metric.md).

Config in `generated/` is picked up within 30 seconds — `--watch-config poll`,
deliberately polling rather than inotify, which does not fire reliably for files
written into a volume by another container. Editing `conf.d/` needs a restart.

## Verify

```bash
source .env
docker compose logs telegraf | grep -E 'Loaded inputs|E!'
curl -sS "localhost:${PROMETHEUS_PORT}/api/v1/query?query=count by (platform)(device_uptime)"
```

## Problems

| Symptom | Cause |
|---|---|
| `translating: MIB search path: ...` | a symbolic OID like `IF-MIB::ifName`. The image ships no MIBs — use numeric OIDs. |
| `request timeout` per agent | SNMP credentials, ACL or firewall — or the device's SNMPv3 level differs from its `snmp_security`. Test with `nc -zvu <ip> 161`. |
| Interface metrics named `device_*`, not `interface_*` | `name_override` in an SNMP template renames the table too — use `name`. A test forbids it |
| New `generated/` files not polled | Telegraf has not loaded them — `make restart SVC=telegraf` |
| Metric has an extra prefix | the field name repeats the measurement — `interface` + `interface_oper_status` gives `interface_interface_oper_status` |
| A device is missing | `make render` not run, or its `status` is not `active` |
| Memory reads absurdly high | wrong `memory.kind` in `platforms.yml` |
| Flow silent | device points at the wrong IP or port; check with `tcpdump -ni any udp port 12055` |
