# Fix silent data loss and device lockout

Four failures this stack is built to avoid. They share one property that makes
them worth their own page: **none of them produce an error.** Nothing crashes,
no container goes unhealthy, no log line appears. Metrics look plausible, logs
keep arriving, a config push reports success — and data is being thrown away.

All four are fixed in the shipped configuration. This page is for recognising
them if they come back after a change, on a host that was set up by hand, or on
a fork.

## Check all four in one go

```bash
source .env

# 1. Kernel dropping UDP at the socket
netstat -su | grep -iE 'receive buffer errors|packet receive errors'

# 2. Telegraf dropping metrics it could not flush
docker compose logs telegraf | grep -icE 'buffer overflow|did not complete within'

# 3 + 4. Repeated logins and overlapping sessions on one device
docker compose logs automation | grep -icE 'authentication failed|connection reset'
```

Three zeros and an empty first line is what healthy looks like. Anything else,
find it below.

---

## 1. UDP receive buffers — syslog and flow vanish

**The most consequential setting on the host.**

### Symptom

Devices are configured and sending. Loki has some logs but fewer than expected,
or flow counters are lower than the device's own statistics. Under load —
a link flap, a reboot storm — the gap widens. Nothing anywhere reports it.

### Detect

```bash
netstat -su | grep -i 'receive buffer errors'
```

A number that climbs is the kernel discarding datagrams before any application
sees them. Then check the ceiling:

```bash
sysctl net.core.rmem_max net.core.rmem_default
```

### Fix

```bash
sudo python3 install.py --fix-sysctl
```

That writes `/etc/sysctl.d/99-darqcube.conf` so it survives a reboot. By hand:

```bash
echo 'net.core.rmem_max = 8388608'     | sudo tee -a /etc/sysctl.d/99-darqcube.conf
echo 'net.core.rmem_default = 1048576' | sudo tee -a /etc/sysctl.d/99-darqcube.conf
sudo sysctl --system
docker compose restart logstash telegraf
```

The collectors must restart — the buffer size is set when the socket is opened.

### Why it is silent

Two mechanisms stacked on top of each other:

1. **UDP has no retransmit.** A dropped datagram is simply gone. The device
   believes it sent; the collector never knew there was anything to receive.
2. **The kernel clamps the request without saying so.** Logstash asks for
   8 MiB via `receive_buffer_bytes`; stock Ubuntu ships
   `net.core.rmem_max = 212992`, so it gets 208 KiB — a 40× reduction, applied
   silently. `setsockopt` returns success.

There is no error path here at all. The only evidence is a counter in
`netstat -su` that nobody looks at, which is why `install.py` and
`scripts/preflight.sh` both check it before you ever start.

---

## 2. Telegraf's metric buffer — metrics dropped on a slow flush

### Symptom

Gaps in Grafana that do not line up with anything. Counters that occasionally
skip an interval. Worse at scale and during Prometheus restarts.

### Detect

```bash
docker compose logs telegraf | grep -i 'buffer overflow'
```

Telegraf logs **one line** when this happens and then carries on.

### Fix

`metric_buffer_limit` must exceed what one interval produces.

```bash
# What one interval actually produces
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=prometheus_tsdb_head_series"
```

Then in `observability/telegraf/telegraf.conf`:

```toml
  metric_buffer_limit = 250000    # ~2 intervals at 400 devices
```

```bash
docker compose restart telegraf
```

Rule of thumb: **at least twice one interval's output.** 400 devices × 30
interfaces produces ~105,000 metrics per interval, so 250,000 gives two
intervals of headroom for a slow or restarting Prometheus.

### Why it is silent

The buffer is a ring. When it fills, the oldest entries are overwritten by new
ones — which is the right behaviour for a metrics agent, because the
alternative is blocking collection. But it means loss is *normal operation*,
not an error condition. One log line, no metric about it, no health change.

---

## 3. Repeated SSH logins — device lockout

### Symptom

Config pushes work in a lab and fail against production. Authentication
failures on a password you know is correct. TACACS or RADIUS logs showing
bursts of logins from the stack. On IOS, `login block-for` locking the account.

### Detect

```bash
docker compose logs automation | grep -iE 'authentication failed|connection refused'
```

And on the device:

```
show login failures            # IOS-XE
display cpu-defend statistics  # VRP
/log print where topics~"account"   # RouterOS
```

### Fix

One operation must mean one SSH session. In `automation/nornir/tasks.py`, the
inventory is cached and a single Nornir instance is passed through every step
of a push:

```python
nr = get_nornir(device)           # ONE instance
try:
    before_config = get_config(device, nr=nr)
    before_state  = snapshot_device(device, nr=nr)
    nr.run(task=netmiko_send_config, config_commands=lines)
    after_state   = snapshot_device(device, nr=nr)
finally:
    nr.close_connections()
```

If you add an operation, **pass `nr` through**. A helper called without it
builds its own instance, which is a fresh inventory fetch and a fresh login.

Verify with the offline test:

```bash
.venv/bin/python -m pytest automation/tests/test_device_sessions.py -v
```

### Why it is silent

Every individual login succeeds. The stack sees four successful operations and
reports success. The damage is on the device side — a rate limiter or an
account lockout — and it only appears once a real AAA policy is in front of the
device. A lab with local credentials and no rate limiting will never show it.

There is a second cost that is invisible either way: each rebuild fetched the
**entire** inventory from Infrahub. At 400 devices, one config push meant four
400-node GraphQL queries.

---

## 4. Shared SSH sessions — interleaved CLI output

### Symptom

Occasional garbage from a device under concurrent use: output from one command
appearing in the result of another, a parse error on a command that normally
works, a TextFSM template "breaking" intermittently. Never reproducible on
demand.

### Detect

Hard to catch after the fact — the evidence is the corrupted output itself.
The test reproduces it deterministically:

```bash
.venv/bin/python -m pytest automation/tests/test_device_sessions.py -k concurrent -v
```

### Fix

Serialise per device, not globally:

```python
_device_locks: dict[str, threading.Lock] = {}

def device_lock(device: str) -> threading.Lock:
    with _locks_guard:
        return _device_locks.setdefault(device, threading.Lock())
```

Every public entry point that touches a device takes its lock; the fleet still
runs concurrently, one device is serialised. Most devices would not accept many
simultaneous sessions from one source anyway.

Two mistakes to avoid:

- **A fresh lock per call locks nothing.** `setdefault` on a shared dict is
  what makes it the *same* lock. There is a test for exactly this.
- **A single global lock is too coarse.** It would serialise the whole fleet
  and turn a 400-device run into a queue. There is a test for that too.

### Why it is silent

This one is worth dwelling on, because **it was introduced while fixing bug 3.**

Caching the inventory means every caller gets a `.filter()` view over the *same*
`Host` objects — and Nornir stores the open connection on the Host. That is
precisely what makes session reuse work. It also means two concurrent requests
for one device share one SSH connection and write into it at the same time.

The fix for one bug created the next. It only appears under concurrent load
against real hardware, produces no exception, and the symptom is a parse failure
that points at the wrong layer entirely — you would go looking at your TextFSM
template.

---

## What these four have in common

**Prometheus was never the bottleneck.** 400 devices is ~105,000 series and
about 400 MB of RAM — comfortable. Every one of these failures is in the
*collectors*, and every one of them is silent. At this scale you do not get
errors, you get quietly missing data, which is why the stack leans on explicit
checks rather than waiting for something to break.

**Two of the four are about a fix creating the next problem.** Sharding SNMP
and caching the inventory were both correct, and both moved the failure
somewhere else. Worth assuming when you change this layer.

**Sharding buys error attribution, not just parallelism.** With one
`[[inputs.snmp]]` holding 400 agents, `did not complete within its interval`
tells you nothing useful. With `SNMP_SHARD_SIZE=150` it tells you which 150
devices are slow — see [../scale.md](../scale.md).

## Related

- [../scale.md](../scale.md) — the 400-device numbers and what to change as you grow
- [troubleshooting.md](troubleshooting.md) — symptoms that *do* produce an error
- [change-settings.md](change-settings.md) — where every tunable lives
