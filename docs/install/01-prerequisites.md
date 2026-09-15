# VM prerequisites

## Sizing

| | Minimum | Comfortable |
|---|---|---|
| vCPU | 4 | 8 |
| RAM | 16 GB | 24 GB |
| Disk | 80 GB | 200 GB |

Where it goes: Neo4j takes a 2 GB heap plus 1 GB page cache, Prometheus grows
with retention and device count, Logstash runs a 512 MB JVM, and the automation
image is ~250 MB on disk.

Rough metric sizing: 50 devices × 30 interfaces × 8 metrics at 30s polling is
~1.5 GB/day. The 15-day default retention is therefore ~20 GB. Raise
`SNMP_INTERVAL` before raising the disk.

## OS

Any Linux with a current Docker. Tested on Ubuntu 24.04. macOS works for
development via Docker Desktop or OrbStack, but syslog and flow need the
device-facing ports reachable from your network.

## Host packages

A fresh Ubuntu Server has almost none of these.

```bash
sudo apt update && sudo apt install -y git make jq python3-venv curl
```

| Package | Needed for | What happens without it |
|---|---|---|
| `git` | cloning and updating the repo | cannot get the code |
| `make` | every documented command | `make: command not found` on the first instruction you follow |
| `jq` | `make state`, `make check`, `make config-get`, `make snapshot`, `make config-parsed` | those commands print nothing and exit non-zero |
| `python3-venv` | `install.py` step 6 builds a venv for the test suite | the install completes but cannot verify itself — and it fails *after* the ~10 minute build |
| `curl` | the verification commands throughout these docs | `curl: command not found` |

`python3` itself ships with Ubuntu Server, but **`python3-venv` is a separate
package** — that catches people out more than anything else here.

`python3 install.py --check` verifies every one of these and changes nothing.

## Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker                       # or log out and back in
```

Verify — **Compose must be v2.20 or newer**, because `compose.yaml` uses
`include:`:

```bash
docker version
docker compose version
```

## Ports

Defaults ship non-standard so the stack can coexist with something already
running. Device-facing ports are the ones that matter:

| Port | Proto | Direction | For |
|---|---|---|---|
| 1514 | UDP | device → VM | syslog |
| 12055 / 14739 | UDP | device → VM | NetFlow / IPFIX |
| 161 | UDP | VM → device | SNMP poll |
| 22 | TCP | VM → device | SSH |
| 57400 | TCP | VM → device | gNMI (Cisco only) |

Port 514 needs root on most Linux hosts, which is why 1514 is the default. To
use 514, set `SYSLOG_PORT=514` and either run Docker as root or grant the
capability.

The VM must be able to reach device management IPs on 161 and 22, and devices
must be able to reach the VM on the syslog and flow ports. Those are different
directions and often different firewall rules.

## Time

```bash
timedatectl set-ntp true
```

Metrics and logs are correlated by timestamp. A VM with a drifting clock makes
a dashboard look wrong in ways that are hard to attribute.

## Check

```bash
./scripts/preflight.sh
```
