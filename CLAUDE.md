# CLAUDE.md

Instructions for Claude Code working in this repo. `README.md` is the document for humans — this one
exists so an agent doesn't rediscover the same constraints by breaking them.

## What this is

A single-VM Docker Compose stack that collects telemetry from network devices, holds their intended
state in Infrahub, automates get/put of configuration, and exposes all of it over MCP so an AI
platform can use it. Cisco IOS-XE, Huawei VRP, MikroTik RouterOS.

Device work is **Netmiko + TextFSM + TTP** throughout, for every vendor. There is one code path,
not one per vendor.

Built for demos, PoCs and small production networks — **sized and tested to 400 devices** on one
Ubuntu box. It is deliberately not hardened: no TLS between components, no approval workflows, no
backup automation. Don't add them without being asked.

`python3 install.py` is the primary install path: one stdlib-only file, seven steps, ending in
pytest against the running stack. Keep it dependency-free — it runs on a freshly cloned repo before
pip has been used, and a test enforces that. That is also why it parses `site.yml` with its own
small reader rather than PyYAML; a test asserts the reader agrees with PyYAML on the example.

**`site.yml` is the per-deployment input** — one file per customer site, gitignored because it
holds credentials. It generates `.env` and nothing else: docs, compose and everything tracked stay
as cloned. Adding a setting means adding it to `site_to_env()` **and** `.env.example`, or it is
silently dropped — there is a test for that.

Four groups, each with its own compose file and top-level directory:

| Group | Directory | Compose file | Does |
|---|---|---|---|
| source-of-truth | `source-of-truth/` | `compose/source-of-truth.yaml` | Infrahub — what should exist |
| observability | `observability/` | `compose/observability.yaml` | Telegraf, Logstash, Prometheus, Loki, Alertmanager, Grafana |
| automation | `automation/` | `compose/automation.yaml` | Nornir, Netmiko, TextFSM, TTP, assurance |
| mcp | `mcp/` | `compose/mcp.yaml` | six servers, one image |

Telegraf ingests SNMP, gNMI and NetFlow/IPFIX. **Logstash ingests syslog** — not Telegraf. That
split is deliberate; don't merge them.

## Golden rules

- **Edit the YAML, not the output.** `observability/telegraf/generated/` is written by
  `render-inventory.py`; anything you put there is lost on the next `make render`.
- **Never hardcode an IP or hostname in a config file.** Everything addressable comes from `.env` or
  from Infrahub via the renderer. This repo gets cloned onto other people's VMs.
- **One credential pair** — `DEVICE_USER` / `DEVICE_PASSWORD` — for Nornir and Netmiko. Don't
  add per-tool variants; they drift apart and then nobody knows which one is real.
- **Device credentials never go in Infrahub.** `.env` only.
- **Adding a vendor is six edits**: `platforms.yml`, a Logstash grok file, a TextFSM template (if
  `ntc-templates` has none), a normaliser in `automation/assurance/normalise.py`, the `platform`
  dropdown in the Infrahub schema, and an onboarding doc. All six, or the vendor is
  half-supported — and half-support fails *silently*, which is worse than not supporting it.

## Commands

Use the Makefile rather than inventing `docker compose` invocations.

| Command | Does |
|---|---|
| `python3 install.py` | the whole install: preflight → configure → build → start → initialise → verify |
| `python3 install.py --from FILE` | generate `.env` from a site file (defaults to `./site.yml`) |
| `python3 install.py --check` | preflight only, changes nothing |
| `make up` | `up -d --wait` — blocks until every container is healthy |
| `make schema` / `make seed` | load the Infrahub schema / apply `devices/*.yml` |
| `make render` | Infrahub → Telegraf + Logstash configs. **Run after any device change.** |
| `make config-get DEV=x` / `config-put DEV=x FILE=y` | get and put device config |
| `make state DEV=x` | parsed operational state (TextFSM) |
| `make check DEV=x` | run the assurance rules |
| `make snapshot DEV=x` | comparable state, for pre/post comparison |
| `make config-parsed DEV=x` | running config parsed with TTP |
| `make test-templates` | TextFSM tests — offline, seconds, no devices |
| `make test` | stack tests — needs the stack up |
| `make test-devices` | needs real devices |
| `make verify` | exactly what `docs/INSTALL.md` says to verify |

## Scale — 400 devices is the tested ceiling

~105,000 Prometheus series at 60s polling. Prometheus is not the constraint; the collectors are.
Three settings exist because of that, and lowering any of them silently loses data:

| Setting | Why |
|---|---|
| `SNMP_SHARD_SIZE=150` | splits the fleet across several `[[inputs.snmp]]` gather loops, so one unreachable device cannot eat the poll window |
| `metric_buffer_limit = 250000` | one interval produces ~105k metrics; a smaller buffer drops the excess on any slow flush |
| `net.core.rmem_max = 8388608` | a **host** sysctl. Devices push syslog and flow over UDP; the kernel silently clamps the 8 MiB the collectors request to 208 KiB on stock Ubuntu and drops the rest with no error |

Full numbers: [docs/scale.md](docs/scale.md). Detection and manual fixes for each:
[docs/how-to/fix-silent-data-loss.md](docs/how-to/fix-silent-data-loss.md).

## Testing

Three layers, because "up" and "working" are different claims:

1. `test_containers.py` — containers running and healthy
2. `test_services.py` — each service's own readiness endpoint answers
3. `test_wiring.py` — components are actually connected to each other (Prometheus targets `up`,
   Grafana datasources healthy, Loki has a `device` label, MCP tools registered)

Layer 3 is the one that catches real problems; everything can be green at layers 1 and 2 while
nothing is connected.

**After touching anything TextFSM, run `make test-templates` first.** It needs no stack and no
devices and finishes in seconds.

## Device work — one path for every vendor

| Layer | Tool | Job |
|---|---|---|
| transport | **Netmiko** | get text off the device, push config to it |
| tabular parsing | **TextFSM** + ntc-templates | `show` output → rows (`automation/textfsm/`) |
| config parsing | **TTP** | running-config → structure (`automation/ttp/`) |
| normalising | `automation/assurance/normalise.py` | three vendors' rows → one shape |
| assurance | `automation/assurance/rules.yml` | declarative checks over the normalised shape |
| comparison | **DeepDiff** | pre/post snapshots → what actually changed |

The normaliser is what makes this vendor-neutral: one rule set covers Cisco,
Huawei and MikroTik. Adding a rule is a YAML edit; adding a vendor needs a
normaliser function.

## Gotchas that cost real time

| Area | Constraint |
|---|---|
| Infrahub schema | every `description:` must be **under 128 characters** — one longer field fails the entire schema load with `string_too_long` and never names the field |
| Infrahub compose | Postgres 18+ mounts at `/var/lib/postgresql`, **not** `/var/lib/postgresql/data` |
| Infrahub compose | Prefect is embedded in the Infrahub image — no separate Prefect image |
| Infrahub API | every attribute comes wrapped as `{"value": x}` — flatten before returning it to a model |
| Infrahub SDK | `prefetch_relationships=True` or `node.site.peer` raises `NodeNotFoundError` |
| Nornir | omit `group_mappings` in the Infrahub inventory plugin — it resolves peers it never fetched, so `slugify()` raises `TypeError` before any host loads |
| Telegraf | `--watch-config poll`, never inotify — inotify is unreliable across a volume mount |
| Loki | labels are `device, site, role, severity` **only**. Message body and Cisco mnemonics stay fields — promoting a mnemonic to a label multiplies stream count by the number of message types |
| Prometheus | never let per-flow IPs or ports become labels; the flow config drops them on purpose |
| TextFSM | an empty parse must **raise** — `[]` and "device has nothing to report" are indistinguishable, so a missing template silently returns a wrong answer |
| Cisco syslog | IOS does **not** emit conformant RFC3164 (counter and hostname come before the timestamp). A strict parser drops every line silently |
| MikroTik SNMP | no vendor CPU MIB — uses HOST-RESOURCES-MIB `hrProcessorLoad` |
| UDP buffers | the most consequential host setting. syslog and flow are UDP: an undersized `net.core.rmem_max` drops datagrams with **no error anywhere**, and the application's larger request is clamped without complaint. Only `netstat -su` shows it |
| install.py | stdlib only — it runs before pip has been used. A test asserts no third-party imports |
| Device sessions | the inventory is cached, so `.filter()` views share `Host` objects and therefore the SSH connection. Every entry point that touches a device takes `device_lock(name)` — without it two concurrent requests interleave on one session. Pass `nr=` through when adding an operation, or you add a login |
| Normalising | a normaliser reading a field the parser does not emit yields plausible but **wrong** booleans. `_require()` in `automation/assurance/normalise.py` exists because RouterOS flags live in `status`, not `flags` — reading the wrong key reported every running interface as down |
| Parsing split | **TextFSM** for tabular `show` output, **TTP** for hierarchical config. Do not point TextFSM at a config file; it has no notion of nested blocks |
| ntc-templates | IOS-XE templates are filed under `cisco_ios`, not `cisco_xe`. That is why `platforms.yml` carries both `netmiko_type` (how to connect) and `textfsm_platform` (how templates are named) |

## Don't

- Add a **passthrough MCP tool** — no raw PromQL, LogQL, GraphQL, or `run_command(device, anything)`.
  Every tool builds its query server-side from bounded arguments. One passthrough tool makes every
  other boundary in the stack decorative.
- **Publish MCP ports.** They are `expose:` only, reached over the Compose network.
- Register `push_device_config` when `MCP_ALLOW_WRITE` is false. It must be absent from `tools/list`,
  not merely refuse when called. Note the flag gates the MCP **tool** only — the automation API
  beneath has no auth, so it is not a system-wide write switch. Don't describe it as one.
- Add a component that isn't in one of the four groups without saying why.
