# Automate with Python

Everything the stack knows and does is reachable over HTTP, so a script needs no
SDK and no access to the containers — the standard library is enough. The
same capabilities are what the MCP servers offer an AI platform; a script and an
agent use one stack.

| Question a script asks | API | Port (`.env`) |
|---|---|---|
| What should exist? — devices, sites, roles | Infrahub GraphQL | `INFRAHUB_PORT` |
| What is the device doing? — state, config, assurance | Automation API | `AUTOMATION_PORT` |
| What happened over time? — metrics | Prometheus HTTP API | `PROMETHEUS_PORT` |
| What did the device log? | Loki HTTP API | `LOKI_PORT` |

Examples use the ports from `.env.example` on `localhost`; from another machine,
use the VM's address.

## Automation API

Interactive reference, with every parameter: `http://<host>:${AUTOMATION_PORT}/docs`.

| Method | Path | Returns |
|---|---|---|
| GET | `/devices` | `{"devices": [{name, hostname, platform, netmiko_type}, …]}` — the live inventory from Infrahub |
| GET | `/device/{name}/state` | parsed operational state (TextFSM); `?raw=1` for the CLI text |
| POST | `/device/{name}/check` | assurance results: `{"results": [{rule, status, detail, …}]}` — `pass`, `fail`, `error` or `skipped` |
| GET | `/device/{name}/snapshot` | comparable state, for pre/post change comparison |
| GET | `/device/{name}/config` | running configuration |
| GET | `/device/{name}/config/structured` | configuration parsed with TTP; `?kind=interfaces` |
| POST | `/device/{name}/config` | push `{"lines": [...]}`; the previous config is archived first |

A failed call returns an HTTP error with `{"detail": "..."}` saying why — an
unreachable device, a device not in Infrahub, a template that does not match.

> **The Automation API has no authentication.** Anything that can reach
> `AUTOMATION_PORT` can push configuration. `MCP_ALLOW_WRITE` gates only the AI
> tool, not this API. Keep the port on a management network.

### Fleet assurance in twenty lines

```python
"""Run the assurance rules on every device and report what needs attention."""
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "http://localhost:18100"          # AUTOMATION_PORT

def call(path, method="GET"):
    req = urllib.request.Request(API + path, method=method)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)

def check(name):
    results = call(f"/device/{name}/check", method="POST")["results"]
    return name, [r["rule"] for r in results if r["status"] in ("fail", "error")]

names = [d["name"] for d in call("/devices")["devices"]]
with ThreadPoolExecutor(max_workers=8) as pool:           # devices in parallel
    for name, bad in pool.map(check, names):
        print(f"{name:12} {'OK' if not bad else 'ATTENTION: ' + ', '.join(bad)}")
```

A check takes seconds per device — longer where pyATS runs — so run devices in
parallel. The stack serialises work on any one device itself, so parallel
calls never share a device session.

### Pre/post change

```python
before = call("/device/cr1/snapshot")
# ... make the change: call("/device/cr1/config", "POST") with a body, or by hand
after = call("/device/cr1/snapshot")
```

Compare the two with any diff tool; the stack's own `make snapshot` output is
the same shape. [DeepDiff](https://pypi.org/project/deepdiff/) reports exactly
what moved.

To push configuration, send a JSON body:

```python
body = json.dumps({"lines": ["interface Loopback100", "description set-by-script"]}).encode()
req = urllib.request.Request(API + "/device/cr1/config", data=body, method="POST",
                             headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=300)))
```

## Intent — Infrahub GraphQL

Every request carries the API token from `.env` (`INFRAHUB_ADMIN_TOKEN`), and
every attribute arrives wrapped as `{"value": x}`:

```python
import json, os, urllib.request

query = "{ NetworkDevice { edges { node { name { value } role { value } site { node { name { value } } } } } } }"
req = urllib.request.Request(
    "http://localhost:18000/graphql",
    data=json.dumps({"query": query}).encode(),
    headers={"Content-Type": "application/json", "X-INFRAHUB-KEY": os.environ["INFRAHUB_ADMIN_TOKEN"]},
)
for edge in json.load(urllib.request.urlopen(req))["data"]["NetworkDevice"]["edges"]:
    n = edge["node"]
    print(n["name"]["value"], n["role"]["value"], n["site"]["node"]["name"]["value"])
```

To **change** intent from a script, write the YAML in
`source-of-truth/devices/` and run `make seed BRANCH=…` — it validates every
record before writing — or use the
[Infrahub Python SDK](https://docs.infrahub.app/python-sdk/) directly. Either
way, `make render` afterwards.

## Metrics — Prometheus

```python
import json, urllib.parse, urllib.request

q = urllib.parse.urlencode({"query": 'interface_oper_status{site="hq"} == 2'})
for r in json.load(urllib.request.urlopen(f"http://localhost:19090/api/v1/query?{q}"))["data"]["result"]:
    print(r["metric"]["device"], r["metric"]["ifName"], "is down")
```

Every series carries `device`, `site`, `role` and `platform` from Infrahub, so a
script can select by intent — every `core` device at one site — without keeping
its own inventory.

## Logs — Loki

```python
import json, time, urllib.parse, urllib.request

q = urllib.parse.urlencode({
    "query": '{role="core"} |= "%SYS-5-CONFIG_I"',       # config changes on core devices
    "start": str(int((time.time() - 3600) * 1e9)),
})
for stream in json.load(urllib.request.urlopen(f"http://localhost:13100/loki/api/v1/query_range?{q}"))["data"]["result"]:
    for ts, line in stream["values"]:
        print(stream["stream"]["device"], line)
```

## Related

- [connect-an-ai-platform.md](connect-an-ai-platform.md) — the same capabilities as MCP tools
- [change-assurance-rules.md](change-assurance-rules.md) — what `/check` evaluates
- [administration/infrahub-guide.md](../administration/infrahub-guide.md) — how devices get into Infrahub
