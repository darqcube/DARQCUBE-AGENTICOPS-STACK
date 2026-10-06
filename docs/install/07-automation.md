# Automation — Nornir, Netmiko, TextFSM, TTP, pyATS

## What it does here

The only component that opens an SSH session to a device. Gets configuration and
operational state, and pushes configuration.

`mcp-netmiko` and `mcp-assurance` front **this API** rather than connecting to
devices themselves, so credentials, parsing and error handling live in exactly
one place.

Without it: all observability continues; you lose get/put, and the two
automation MCP servers with it.

## The tools, and why each is here

| Tool | Role |
|---|---|
| **Nornir** | inventory and concurrency. Reads Infrahub live, so there is no inventory file to drift. |
| **Netmiko** | the CLI transport — through Nornir for fleet runs, directly in the standalone scripts. |
| **TextFSM** + ntc-templates | tabular `show` output → rows. This is what makes "get state" return JSON rather than a wall of text. |
| **TTP** | hierarchical *config* → structure. A running-config is a tree, and TextFSM has no notion of nested blocks. |
| **normalise.py** | flattens three vendors' rows into one shape, so one rule set covers the fleet. |
| **pyATS / Genie** | structured `learn()` models and parsers, used where Genie supports the platform — see below. |
| **DeepDiff** | compares pre/post snapshots, so a config push reports what it actually changed. |
| **rich** | readable output from the standalone scripts. |

### Two engines, and pyATS is additive

**TextFSM** handles interface state for every platform. The three vendors
describe it in three incompatible ways; `normalise.py` maps all of them to
`{interface, admin_up, oper_up}` so one set of interface rules covers the fleet.

**pyATS / Genie** runs on top, wherever Genie genuinely returns data:

| Platform | pyATS does | How |
|---|---|---|
| Cisco IOS-XE | BGP session state | `learn("bgp")` — a full Genie model |
| Huawei VRP | BGP session state | `parse("display bgp peer")` — Genie's hvrp library is BGP parsers only |
| MikroTik | nothing | unicon has no RouterOS plugin, Genie no parsers |

Declared per platform in `platforms.yml` under `pyats:`. A platform is never
assured by pyATS alone — if Genie turned out to cover less than assumed, the
platform would still have every interface check.

**Huawei is the case to understand.** pyATS connects to it (unicon `hvrp`) and
Genie parses its BGP output — but there is no hvrp interface parser and no hvrp
`learn()` model at all. `learn("interface")` against a VRP device connects
successfully and returns nothing. That is why Huawei interface checks stay on
TextFSM, and why a test fails if anyone declares `learn:` under `vrp`.

pyATS opens its own SSH session through unicon, separate from Netmiko's. The
assurance run holds the device lock across both, so they never overlap on one
device.

The per-vendor mappings and how to add a rule:
[../how-to/change-assurance-rules.md](../how-to/change-assurance-rules.md).

## Configuration

| Where | What |
|---|---|
| `automation/nornir/tasks.py` | get/put/state, and the Nornir inventory wiring |
| `automation/netmiko/*.py` | standalone single-device scripts |
| `automation/textfsm/` | parsing: `parse.py`, `index`, `templates/`, `samples/` |
| `automation/ttp/` | TTP config parsing: `parse.py`, `templates/` |
| `automation/assurance/` | `normalise.py`, `engine.py`, `rules.yml` |
| `automation/pyats/` | `testbed.py`, `checks.py`, and `samples/` — Genie's own golden fixtures |
| `automation/service/main.py` | the HTTP API |
| `automation/service/scheduler.py` | scheduled assurance — runs the rules on every device and exposes the results |
| `.env` → `DEVICE_USER` / `DEVICE_PASSWORD` | one pair, used by all of the above |
| `.env` → `AUTOMATION_CONCURRENCY` | devices talked to at once (default 8) |
| `.env` → `ASSURANCE_INTERVAL_MINUTES` | scheduled assurance every N minutes (default 0 = off) |
| `.env` → `MAX_CONFIG_LINES` | cap on a single push (default 200) |
| `.env` → `AUTOMATION_PORT` | published port (default 18100) |

## How an assurance check runs — and where the code is

Every way of asking for assurance ends in one function, `run_assurance()` in
`automation/nornir/tasks.py`, so a rule behaves the same whether a person, an
AI agent or the scheduler asked:

```
    make check DEV=router1 ─┐
POST /device/router1/check ─┤
 mcp-assurance (AI agents) ─┼─> run_assurance("router1")    automation/nornir/tasks.py
scheduler, every N minutes ─┘     │  holds the device lock across both engines
                                  ├─ Netmiko + TextFSM ─> normalise.py      ─> rules, source: interfaces
                                  └─ pyATS / Genie ─────> pyats/checks.py   ─> rules, source: pyats
                                  │
                                  v
                one result per rule: pass | fail | error | skipped
                                  │
    API and make check return it  │  the scheduler also publishes it:
                                  v
          /metrics ─> Prometheus ─> Grafana assurance panels + alerts
          /assurance/latest      ─> JSON, with each failure's detail
```

| File | What it is | Change it to |
|---|---|---|
| `automation/assurance/rules.yml` | the rules: name, severity, platforms, which check, its options | add, remove or tune a rule — the usual change |
| `automation/assurance/engine.py` | runs the rules (`run_rules`); the interface checks, registered in `CHECKS`; pre/post snapshots | add a new kind of interface check |
| `automation/assurance/normalise.py` | turns each vendor's interface rows into `{interface, admin_up, oper_up}` | support a new platform's interface output |
| `automation/pyats/checks.py` | collects Genie data (`collect`) and the pyATS checks, registered in `CHECKS` | add a pyATS-backed check |
| `automation/pyats/testbed.py` | builds the pyATS testbed from the cached Infrahub inventory — there is no testbed file | rarely: connection settings |
| `automation/pyats/samples/` | Genie's own golden output, for the offline tests | add the fixture a new pyATS check is tested against |
| `platforms.yml` → `pyats:` | which Genie features each platform supports | enable a pyATS rule on a platform |
| `automation/nornir/tasks.py` → `run_assurance()` | the single entry point; gathers both engines' data under the device lock | rarely |
| `automation/service/main.py` | the HTTP API, including `POST /device/{name}/check` | a new endpoint |
| `automation/service/scheduler.py` | scheduled assurance: runs every device, serves `/metrics` and `/assurance/latest` | the published metrics |
| `mcp/servers/assurance.py` | the `mcp-assurance` tools AI agents call (`run_device_checks` and others) — through the API above | what agents can ask |
| `observability/prometheus/rules/` | `network:assurance_*` counts and the `AssuranceCheckFailing` / `AssuranceStale` alerts | alerting on results |

### Common changes, step by step

| You want to | Edit | Then | Prove it |
|---|---|---|---|
| Add or tune a rule using an existing check | `automation/assurance/rules.yml` | nothing — it is read on every run | `make check DEV=<device>` |
| Add a new kind of interface check | `automation/assurance/engine.py` (function + `CHECKS`), then a rule in `automation/assurance/rules.yml` | `docker compose restart automation` | `.venv/bin/pytest automation/tests/test_assurance.py` |
| Add a pyATS check | `automation/pyats/checks.py` (function + `CHECKS`), a fixture in `automation/pyats/samples/`, a rule with `source: pyats` | `docker compose restart automation` | `.venv/bin/pytest automation/tests/test_pyats.py` |
| Run it on another platform | `platforms.yml` → that platform's `pyats:` block | `docker compose restart automation` — `platforms.yml` is a single-file mount, and a running container keeps the old copy | `make check DEV=<device>` |
| Cover a new vendor's interfaces | `automation/assurance/normalise.py` + a TextFSM template ([../how-to/add-a-platform.md](../how-to/add-a-platform.md)) | `docker compose restart automation` | `.venv/bin/pytest automation/tests/test_assurance.py` |
| See results on the dashboards | `.env` → `ASSURANCE_INTERVAL_MINUTES` | `docker compose up -d automation` | Network Overview → assurance row |

Python changes need the restart because the API does not reload code; the
`automation/` folder is mounted, so no image rebuild is needed. Only a new
Python dependency (`automation/service/requirements.txt`) needs a rebuild:
`docker compose build automation && docker compose up -d automation`.

Rules, checks and examples in detail:
[../how-to/change-assurance-rules.md](../how-to/change-assurance-rules.md).

## API

| Endpoint | Does |
|---|---|
| `GET /devices` | inventory as the automation layer sees it |
| `GET /device/{name}/config` | running config, archived to `automation/configs/` |
| `GET /device/{name}/state` | parsed operational state |
| `GET /device/{name}/state?raw=1` | unparsed CLI output — how you capture a TextFSM sample |
| `GET /device/{name}/config/structured` | running config parsed with TTP |
| `GET /device/{name}/snapshot` | comparable state, for pre/post comparison |
| `POST /device/{name}/config` | push config lines (reports what changed) |
| `POST /device/{name}/check` | run the assurance rules — TextFSM on every platform, pyATS where supported |
| `GET /assurance/latest` | the latest scheduled results, with each rule's detail |
| `GET /metrics` | the same results in Prometheus format — scraped as job `automation` |

Or from the Makefile:

```bash
make config-get DEV=router1
make state DEV=mt-01
make config-parsed DEV=router1
make snapshot DEV=router1
make config-put DEV=router1 FILE=change.txt
make check DEV=router1
```

Or as scripts, which take the same path through the same functions:

```bash
docker compose exec automation python -m automation.netmiko.get_state --device mt-01
docker compose exec automation python -m automation.netmiko.put_config --device router1 --file /tmp/c.txt --dry-run
```

## Assurance is declarative

`automation/assurance/rules.yml` holds the checks. They run against normalised
state, so one rule covers Cisco, Huawei and MikroTik:

```yaml
  - name: admin_up_interfaces_are_operational
    description: An interface the operator has enabled should be carrying traffic.
    severity: error
    applies_to: all
    check: admin_up_means_oper_up
    ignore: '^(Loopback|Null|Vlan|lo|bridge)'
```

Adding a rule is a YAML edit. Adding a *platform* needs a normaliser function in
`automation/assurance/normalise.py` — see
[../how-to/add-a-platform.md](../how-to/add-a-platform.md).

## Scheduled assurance

On demand, assurance answers "is this device right *now*?". Scheduled, it
becomes a history: which rule failed, on which device, since when — in
Prometheus, alertable and on the dashboards.

```bash
# .env
ASSURANCE_INTERVAL_MINUTES=15
docker compose up -d automation
```

Every interval the service runs the rules on every device in the inventory,
`AUTOMATION_CONCURRENCY` at a time, through the same `run_assurance` the API
uses — so a scheduled result and `make check` can never disagree.

It is **off by default** because each run opens an SSH session (and, where
pyATS is declared, a unicon session) to every device. Pick an interval your
devices' session limits and AAA logs tolerate; 15 minutes is a reasonable
start. One run takes roughly `devices ÷ AUTOMATION_CONCURRENCY × ~30 s`.

| Metric | Meaning |
|---|---|
| `assurance_rule_state{device, rule, severity, source, state}` | `1` for the current state: `pass`, `fail`, `error` or `skipped` |
| `assurance_device_run_ok{device}` | `1` the run completed, `0` it could not (unreachable, login failed) |
| `assurance_device_last_run_timestamp_seconds{device}` | when the device was last assured |
| `assurance_device_run_duration_seconds{device}` | how long that took |
| `assurance_interval_seconds` | the configured interval (`0` = off) |

Labels are bounded — devices × rules. *Which* interface or peer failed stays
in `GET /assurance/latest`, never in a label. Alerts `AssuranceCheckFailing`
and `AssuranceStale` and the dashboards' assurance panels read these.

The scheduler lives in the API process, and the image runs one uvicorn worker.
Adding `--workers` would start one scheduler per worker, each assuring every
device.

## Import rule

`automation/nornir/`, `automation/textfsm/` and `automation/ttp/` are named
after the libraries they wrap, so they **shadow** those libraries if
`automation/` is ever on `sys.path`. Only the repo root goes on the path, and
everything imports absolutely:

```python
from automation.nornir import tasks      # correct
from automation.textfsm import parse     # correct
import parse                             # WRONG — breaks the real textfsm
```

## Three honesty guards

**An empty parse raises** rather than returning `[]`. TextFSM returns an empty
list for a template that does not match, and `[]` is indistinguishable from "the
device has nothing to report" — so a broken template would look exactly like a
healthy idle device.

**A normaliser that reads a missing field raises.** `_require()` checks the
fields it is about to read. Without it, a renamed parser field reads as `""` and
the booleans come out plausible but *wrong* — worse than an error, because the
result still looks like data. This was a real bug: RouterOS flags live in
`status`, not `flags`.

**A comparison against an empty snapshot raises.** An empty side diffs clean
against anything, reporting "no change" when nothing was actually checked.

## Verify

With scheduled assurance on, the first run starts at boot: `docker compose logs automation | grep assurance:` prints `assurance: N device(s) in Ns`, and `curl -s localhost:${AUTOMATION_PORT}/metrics | grep -c assurance_rule_state` counts devices × rules.

```bash
source .env
curl -sS "localhost:${AUTOMATION_PORT}/healthz"
curl -sS "localhost:${AUTOMATION_PORT}/devices" | .venv/bin/python -m json.tool
make state DEV=<device>
make test-templates
```

## Problems

| Symptom | Cause |
|---|---|
| `template matched no lines` | no TextFSM template — [add one](../how-to/add-a-textfsm-template.md) |
| `no template for cisco_xe` | ntc-templates files IOS-XE under `cisco_ios`. `platforms.yml` has `textfsm_platform` for exactly this. |
| Authentication failed | `DEVICE_USER`/`DEVICE_PASSWORD`, or the device's SSH config |
| `TypeError` in `slugify` at startup | `group_mappings` was added to the Nornir Infrahub inventory — it must stay out |
| Device not found | not in Infrahub, or `status` is not `active` |
| `no interface normaliser for platform` | add one in `automation/assurance/normalise.py` |
| `parsed row is missing [...]` | the TextFSM template and the normaliser disagree on field names |
| `no TTP template for ...` | add one in `automation/ttp/templates/<platform>-<kind>.txt` |
