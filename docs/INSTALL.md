# Install

Two ways. The installer does the whole thing; the manual path is the same steps
if you would rather run them yourself.

---

## 0. Prepare the host

A fresh Ubuntu Server has almost none of this. One command:

```bash
sudo apt update && sudo apt install -y git make jq python3-venv curl
```

| | Why |
|---|---|
| `git` | clone and update the repo |
| `make` | every documented command — `make up`, `make seed`, `make render` |
| `jq` | `make state`, `make check`, `make config-get` format their output with it |
| `python3-venv` | **a separate package on Ubuntu.** `install.py` step 6 cannot create its test environment without it |
| `curl` | used by the docs' verification commands |

Then Docker, with the Compose v2 plugin:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker            # or log out and back in
docker compose version   # must be 2.20 or newer — compose.yaml uses `include:`
```

And the kernel setting that decides whether syslog and flow survive:

```bash
sudo python3 install.py --fix-sysctl
```

> Devices push syslog and flow over **UDP**, which has no retransmit. Stock
> Ubuntu's socket buffer is 208 KiB against the 8 MiB the collectors request,
> and the kernel clamps the request **silently** — no error, no log line. This
> writes `/etc/sysctl.d/99-darqcube.conf` so it survives a reboot.

`install.py --check` verifies all of the above and changes nothing.

---

## 1. The short way

```bash
git clone <this repo> && cd DARQCUBE-AGENTICOPS-STACK
cp site.example.yml site.yml && chmod 600 site.yml
$EDITOR site.yml                  # the ten values below
python3 install.py
```

That is the whole installation. It runs seven steps and stops at the first one
that cannot succeed:

| | Step | Does |
|---|---|---|
| 1 | preflight | OS, Docker, Compose version, RAM, disk, ports, kernel UDP buffers |
| 2 | configure | creates `.env`, generates the secrets, prompts for the five values only you know |
| 3 | build | builds the Logstash and automation images |
| 4 | start | `docker compose up -d --wait` — blocks until every container is healthy |
| 5 | initialise | loads the Infrahub schema, seeds devices, renders the collector configs |
| 6 | verify | creates a venv and runs pytest against the running stack |
| 7 | report | where everything is, and what to point your devices at |

Re-running is safe. Nothing that already holds a real value is overwritten, so
after fixing whatever it complained about you just run it again.

```bash
python3 install.py --check        # preflight only, changes nothing
python3 install.py --yes          # never prompt (fails instead of asking)
python3 install.py --skip-tests   # stop after step 5
python3 install.py --step 6       # run one step on its own
```

### Before you start

- **Ubuntu Linux** (22.04 or 24.04), 8 vCPU / 24 GB RAM / 200 GB disk for the
  full 400-device ceiling. A lab of a dozen devices runs happily on 4 / 16 / 80.
- **Docker Engine 24+** with the Compose v2.20+ plugin — `compose.yaml` uses
  `include:`. Install: `curl -fsSL https://get.docker.com | sh`
- **Kernel UDP buffers.** Devices push syslog and flow over UDP, and an
  undersized socket buffer drops datagrams with no error and no retransmit.
  The installer checks this and can fix it:

  ```bash
  sudo python3 install.py --fix-sysctl
  ```

  It writes `/etc/sysctl.d/99-darqcube.conf` so the setting survives a reboot.
  Stock Ubuntu ships `net.core.rmem_max = 212992`; the stack needs 8388608.

---

## 2. Fill out `site.yml`

One file describes one deployment. `install.py` reads it and generates `.env`;
**nothing else in the repository is touched**, so `git pull` stays clean and the
documentation you are reading never becomes customer-specific.

```bash
cp site.example.yml site.yml
chmod 600 site.yml
```

> **`site.yml` contains credentials.** It is gitignored, and `install.py`
> refuses to run if it has been committed anyway. Treat it as a credential:
> don't paste it into a ticket, and delete it when the engagement ends.

### What to fill in

| Field | What it is | Where to get it | If it's wrong |
|---|---|---|---|
| `site.name` | short name for this deployment | you choose — e.g. `acme-hq` | appears as a label on every metric |
| `site.collector_ip` | **this VM's routable IP** | `ip -4 addr show scope global` | **the most common mistake.** Devices send syslog and flow to an address nothing is listening on. Neither end reports an error — `install.py` checks the address is actually on this host |
| `site.device_subnets` | the management subnets your devices live on | the customer's network team | only used to sanity-check reachability; nothing is configured from it |
| `devices.ssh_user` / `ssh_password` | the account the stack uses to SSH to every device | create it on the devices first — see [devices/](devices/) | automation cannot fetch or push configuration |
| `devices.snmpv3.user` / `auth` / `priv` | SNMPv3 credentials | must match exactly what you set on the devices | no metrics at all; SNMP failures look like timeouts |
| `devices.gnmi_user` / `gnmi_password` | optional, Cisco IOS-XE only | leave blank unless using `telemetry_mode: gnmi` | — |
| `scale.expected_devices` | roughly how many devices this site polls | the customer's inventory | picks polling interval, SNMP shard size and concurrency for you — see below |
| `scale.metrics_retention` / `logs_retention` | how long to keep data | disk you have | ~105,000 series at 60s is about 4 GB per 15 days |
| `ports.standard` | `true` for the usual ports, `false` for the non-colliding set | whether anything else uses 3000/8000/514 on this host | port conflicts at startup; `syslog` on 514 also needs root |
| `alerts.webhook_url` | where alerts are delivered | optional | empty means alerts stay in the Alertmanager UI — no external dependency |
| `ai_platform.enabled` | run the six MCP servers | optional | `false` removes no other feature |
| `ai_platform.allow_write` | may an AI push configuration to devices | **leave `false` unless you mean it** | `true` registers the write tool; `false` means it does not exist to be called |

### `expected_devices` does the tuning for you

It sets three things you would otherwise get right by reading
[scale.md](scale.md) and doing arithmetic:

| Devices | Polling | SNMP shard | Automation concurrency |
|---|---|---|---|
| up to 50 | 30s | 150 | 8 |
| up to 400 | 60s | 150 | 16 |
| over 400 | 120s | 200 | 24 — and it warns you, this is past the tested ceiling |

### Re-running is safe

`install.py` reads `site.yml` every time and regenerates `.env`, but **keeps the
seven secrets it already generated** — regenerating those would orphan the data
already in Neo4j and Postgres. Change a value in `site.yml`, re-run, and only
that value moves.

### If you would rather not use a site file

`install.py` prompts for the five values it cannot generate and puts the rest on
defaults. Fine for a one-off; the site file is better when you deploy more than
once, because it is also the record of what you deployed.

---

## 3. The manual way, if you prefer

Same steps, run yourself. Useful if you want to understand what the installer
is doing, or to do it piecemeal.

```bash
# 1. preflight
./scripts/preflight.sh

# 2. configure
./scripts/gen-secrets.sh          # .env with generated secrets
$EDITOR .env                      # fill in the five values above

# 3 + 4. build and start
make up                           # build + up -d --wait

# 5. initialise
make schema                       # load the Infrahub schema
$EDITOR source-of-truth/devices/devices.yml   # your devices
make seed                         # devices.yml -> Infrahub
make render                       # Infrahub -> Telegraf and Logstash

# 6. verify
make test
```

**`make render` is the step people forget.** Without it a device exists in
Infrahub and is polled by nothing.

---

## 4. Then configure the devices

Each device needs an SSH user, SNMPv3, a syslog destination and flow export:

- [Cisco IOS-XE](devices/cisco-ios-xe.md)
- [Huawei VRP](devices/huawei-vrp.md)
- [MikroTik RouterOS](devices/mikrotik-routeros.md)

**The device's hostname must exactly match its `name` in `devices.yml`** — it
is the key that joins a log line to a metric to an Infrahub record.

```bash
make test-devices     # once devices are configured
```

---

## Scale

Sized and tested to **400 devices**. See [scale.md](scale.md) for the numbers,
what changes as you grow, and where the ceiling actually is.

Defaults in `.env.example` are the 400-device settings. For a small lab you can
drop `SNMP_INTERVAL` to `30s` for a livelier demo.

---

## Ports

Non-standard by default (Grafana 13000, Infrahub 18000, …) so the stack can run
alongside something already using the usual ones. To use standard ports, set
each to the value in its comment or delete the ports section of `.env` —
[how-to/change-ports.md](how-to/change-ports.md).

---

## Day to day

```bash
make help               # every command, with a one-line description
make install            # same as: python3 install.py
make preflight          # check the host without changing anything
make ps                 # what is running
make logs SVC=grafana   # follow one service
make restart SVC=telegraf   # restart one service after a config edit
make down               # stop, keep all data
make clean              # stop and DELETE all data (asks first)
```

If something is missing: [how-to/troubleshooting.md](how-to/troubleshooting.md).

## Component reference

| Component | Page |
|---|---|
| VM prerequisites, Docker | [install/01-prerequisites.md](install/01-prerequisites.md) |
| Infrahub — the source of truth | [install/02-infrahub.md](install/02-infrahub.md) |
| Telegraf — SNMP, gNMI, flow | [install/03-telegraf.md](install/03-telegraf.md) |
| Prometheus and Loki — storage | [install/04-prometheus-loki.md](install/04-prometheus-loki.md) |
| Logstash — syslog | [install/05-logstash.md](install/05-logstash.md) |
| Grafana and Alertmanager | [install/06-grafana-alertmanager.md](install/06-grafana-alertmanager.md) |
| Automation — Nornir, Netmiko, TextFSM, TTP | [install/07-automation.md](install/07-automation.md) |
| MCP servers | [install/08-mcp.md](install/08-mcp.md) |
