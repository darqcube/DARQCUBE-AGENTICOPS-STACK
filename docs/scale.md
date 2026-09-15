# Scale

The stack is sized and tested to **400 network devices** on one Ubuntu VM.
This page is the arithmetic behind that number, what to change as you grow into
it, and where the real ceiling is.

## The envelope

400 devices averaging 30 interfaces each:

| | |
|---|---|
| Interface series | 84,000 (400 × 30 × 7 fields) |
| CPU (one per core) | ~1,600 |
| Memory | ~1,200 |
| Uptime | 400 |
| Flow (bounded by design) | ~16,800 |
| **Active Prometheus series** | **~105,000** |

At the default 60s polling that is ~1,700 samples/sec and roughly **4 GB of
metrics per 15 days**. Prometheus holds ~105k series in about 400 MB of RAM.

**Prometheus is not the constraint.** It would take a million series before the
TSDB became interesting. The things that actually break first are all in the
collectors.

## Host sizing

| | 400 devices | a lab of a dozen |
|---|---|---|
| vCPU | 8 | 4 |
| RAM | 24 GB | 16 GB |
| Disk | 200 GB | 80 GB |

Neo4j takes a 2 GB heap plus 1 GB page cache regardless of fleet size — it is
sized for Infrahub, not for the number of devices. Prometheus grows with
retention; Logstash runs a fixed 512 MB JVM.

## What actually breaks first

Each of these has a detection command and a manual fix in
[how-to/fix-silent-data-loss.md](how-to/fix-silent-data-loss.md). They are
grouped together there because they share the property that matters: none of
them produce an error.

### 1. UDP receive buffers — silent data loss

The single most consequential setting. Devices *push* syslog and flow over UDP,
which has no retransmit. When the kernel's socket buffer overflows, datagrams
are discarded with **no error, no log line, and nothing in the application**.
The only evidence is a rising `RcvbufErrors` in `netstat -su`, which nobody
looks at.

Worse, the application's request for a bigger buffer is *silently clamped* to
the kernel ceiling. Logstash asks for 8 MiB; stock Ubuntu gives it 208 KiB and
says nothing.

```bash
sudo python3 install.py --fix-sysctl      # sets and persists both values
```

```
net.core.rmem_max     = 8388608     # stock Ubuntu: 212992
net.core.rmem_default = 1048576     # stock Ubuntu: 212992
```

Check it is holding:

```bash
netstat -su | grep -i 'receive buffer errors'    # should stay at 0
```

### 2. Telegraf's metric buffer

One interval at 400 devices produces ~105,000 metrics. If
`metric_buffer_limit` is smaller than that, a single slow flush to Prometheus
drops the excess — Telegraf logs one "metric buffer overflow" line and carries
on as if nothing happened.

Set to **250,000** in `observability/telegraf/telegraf.conf`, about two
intervals of headroom.

### 3. SNMP poll window

A timeout plus one retry is 20 seconds. At a 30s interval, one unreachable
device is close to overrunning the whole cycle, and Telegraf logs
`did not complete within its interval` and skips samples.

Two mitigations, both on by default:

- **`SNMP_INTERVAL=60s`** — the fleet setting. 30s is fine for a lab.
- **`SNMP_SHARD_SIZE=150`** — `render-inventory.py` splits the fleet across
  several `[[inputs.snmp]]` instances. Each is its own gather loop, so one
  unreachable group cannot eat the whole window, and a Telegraf error names
  *which* shard is slow.

At 400 devices that renders as 3 interface shards plus one input per platform —
6 independent gather loops.

```bash
make render
ls observability/telegraf/generated/
# snmp-interfaces-1.conf  snmp-interfaces-2.conf  snmp-interfaces-3.conf
# snmp-ios_xe.conf        snmp-vrp.conf           snmp-routeros.conf
```

### 4. Loki stream count

Labels are `device`, `site`, `role`, `severity`, `platform`. Because a device
determines its own site, role and platform, the real stream count is roughly
**devices × severities** — 400 × 8 = 3,200 streams. Comfortable.

This is only true while the label set stays fixed. Promoting a per-message
field (a Cisco mnemonic, a RouterOS topic) multiplies stream count by the
number of distinct message types and will make Loki unusable.
`max_label_names_per_series: 12` in `loki.yml` is the backstop, and a test
enforces the label list.

### 5. Automation concurrency

`AUTOMATION_CONCURRENCY=16` — each one is an SSH session. A fleet-wide config
fetch across 400 devices is 25 rounds. Raise it if your devices tolerate more
concurrent sessions; the host is rarely the limit.

## Growing past 400

In rough order of what to do first:

1. **Raise `SNMP_INTERVAL`** to 120s. Halves the sample rate and the poll
   pressure, and for interface counters it changes very little.
2. **Lower `PROM_RETENTION`**, or add remote write to long-term storage.
3. **Split Telegraf.** One container currently does SNMP, gNMI and flow. Give
   flow its own container so an SNMP gather spike cannot cause UDP loss —
   they are in one process today only because it keeps the demo simple.
4. **Move Prometheus off the box**, or shard it by site.

Beyond roughly 1,000 devices the single-VM shape stops being the right answer,
and the question becomes which component to move first rather than how to tune
it.

## Measuring your own install

```bash
source .env

# Active series — the number that matters
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=prometheus_tsdb_head_series"

# Is Telegraf keeping up?
docker compose logs telegraf | grep -iE 'buffer overflow|did not complete'

# Is anything being dropped at the socket?
netstat -su | grep -iE 'receive buffer|packet receive errors'

# Loki stream count
curl -s "localhost:${LOKI_PORT}/loki/api/v1/labels"
```

Three of those returning nothing interesting is what healthy looks like.
