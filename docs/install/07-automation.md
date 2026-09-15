# Automation — Nornir, Netmiko, TextFSM, TTP

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
| **DeepDiff** | compares pre/post snapshots, so a config push reports what it actually changed. |
| **rich** | readable output from the standalone scripts. |

### One code path for every vendor

The three vendors describe interface state in three incompatible ways.
`normalise.py` maps all of them to `{interface, admin_up, oper_up}` before any
rule runs, so the assurance layer is one implementation rather than one per
vendor. The per-vendor mapping is documented where you need it —
[../how-to/change-assurance-rules.md](../how-to/change-assurance-rules.md).

## Configuration

| Where | What |
|---|---|
| `automation/nornir/tasks.py` | get/put/state, and the Nornir inventory wiring |
| `automation/netmiko/*.py` | standalone single-device scripts |
| `automation/textfsm/` | parsing: `parse.py`, `index`, `templates/`, `samples/` |
| `automation/ttp/` | TTP config parsing: `parse.py`, `templates/` |
| `automation/assurance/` | `normalise.py`, `engine.py`, `rules.yml` |
| `automation/service/main.py` | the HTTP API |
| `.env` → `DEVICE_USER` / `DEVICE_PASSWORD` | one pair, used by all of the above |
| `.env` → `AUTOMATION_CONCURRENCY` | devices talked to at once (default 8) |
| `.env` → `MAX_CONFIG_LINES` | cap on a single push (default 200) |
| `.env` → `AUTOMATION_PORT` | published port (default 18100) |

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
| `POST /device/{name}/check` | run the assurance rules — **every platform** |

Or from the Makefile:

```bash
make config-get DEV=cr1
make state DEV=mt-01
make config-parsed DEV=cr1
make snapshot DEV=cr1
make config-put DEV=cr1 FILE=change.txt
make check DEV=cr1
```

Or as scripts, which take the same path through the same functions:

```bash
docker compose exec automation python -m automation.netmiko.get_state --device mt-01
docker compose exec automation python -m automation.netmiko.put_config --device cr1 --file /tmp/c.txt --dry-run
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

```bash
source .env
curl -s "localhost:${AUTOMATION_PORT}/healthz"
curl -s "localhost:${AUTOMATION_PORT}/devices" | .venv/bin/python -m json.tool
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
