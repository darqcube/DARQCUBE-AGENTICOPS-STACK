# Run on an existing Docker engine

Run the stack as plain containers on a Docker engine you already have —
OrbStack or Docker Desktop on a laptop, a shared Docker host — without a
dedicated VM and without `install.py`.

`compose.yaml` needs nothing from the installer: no host networking, no
privileged containers, no kernel settings, and the three local images
(`logstash`, `automation`, `mcp`) are built on the first `up`. `install.py` is a
convenience wrapper; this page is the same steps by hand.

## 1. Check the engine

```bash
docker compose version                                       # 2.20 or newer — compose.yaml uses include:
docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes'  # see prerequisites for sizing
docker run --rm alpine cat /proc/sys/net/core/rmem_max       # UDP buffer ceiling
```

The last one matters most. The collectors ask for 8 MiB UDP buffers; the kernel
silently clamps the request to this value, and syslog and flow over the limit
are dropped with **no error anywhere**. `install.py --fix-sysctl` raises it on a
Linux host, but it cannot reach the kernel behind a desktop Docker engine:

| Engine | Typical `rmem_max` | |
|---|---|---|
| OrbStack | ~7.5 MB | fine |
| Docker Desktop, stock Linux | 212992 (208 KB) | lossy under load — raise it in the engine's VM, or use TCP syslog where the device supports it |

## 2. Create `.env`

From the repo folder. Either let the installer do only this step:

```bash
cp site.example.yml site.yml && chmod 600 site.yml && ${EDITOR:-nano} site.yml
python3 install.py --step 2          # configure only — writes .env, starts nothing
```

or by hand: `cp .env.example .env`, then replace each `CHANGEME` with a random
value — the installer uses 48 hex characters:

```bash
python3 -c 'import secrets; print(secrets.token_hex(24))'
```

and fill in the device credentials and SNMPv3 settings. Keep
`COMPOSE_PROFILES=devices,automation,mcp` — without `devices`, Telegraf does not
start and nothing is collected.

To give the containers their own group name in the engine's UI, add:

```bash
COMPOSE_PROJECT_NAME=agenticops-stack     # default: darqcube
```

## 3. Start and initialise

```bash
make up          # builds the local images, starts everything, waits for healthy
make schema
make seed        # when source-of-truth/devices/*.yml exist
make render
```

Tests need a virtual environment — the list `install.py` step 6 installs:

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pyyaml requests textfsm ntc-templates ttp deepdiff
make test
```

## 4. Reaching devices in a container lab

If the devices are containers on the same engine — containerlab, for example —
the stack's network (`darqcube`) and the lab's management network are separate
Docker networks, and **Docker blocks traffic between them**. Attach the three
services that talk to devices to the lab network with a local override. Docker
Compose merges `compose.override.yaml` automatically, and every `make` target
uses plain `docker compose`:

```yaml
# compose.override.yaml — local and gitignored; lab-specific values stay here
services:
  telegraf:                        # polls devices: SNMP, gNMI
    networks: [darqcube, lab]
  automation:                      # SSH to devices
    networks: [darqcube, lab]
  logstash:                        # receives syslog from devices
    networks:
      darqcube: {}
      lab:
        ipv4_address: <free address in the lab subnet>   # the devices' logging host

networks:
  lab:
    external: true
    name: <lab management network>   # docker network ls
```

Devices then send syslog to Logstash's lab address on port 514 — the container
port, since nothing is published on that network — and `management_ip` in the
inventory is the device's lab address.

**Keep the lab and the stack on the same engine.** Traffic between a desktop
Docker engine and a separate VM crosses a virtualisation boundary that can lose
UDP datagrams in bursts — measured at ~7% of a 200-line burst, with none lost
within one engine. TCP syslog survives that, but an emulated device's slow
retransmit timers can delay it by minutes.

Device images built for x86 only (Cisco IOL, for example) run on Apple Silicon
only where the engine provides Rosetta, as OrbStack does — not in an ARM VM
under a hypervisor without it.

## 5. What you give up

Compared with the installer on a dedicated Ubuntu VM: no preflight of host
resources and kernel settings, and no automatic end-to-end test after install —
run `make verify` and `make test` yourself. The stack is otherwise identical.
