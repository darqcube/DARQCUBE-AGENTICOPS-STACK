# Label NetFlow by application

Flows normally say **which device** saw traffic, of which protocol. Once the
services your applications listen on are in Infrahub, every flow also says
**which application** it belongs to — and how critical that application is —
without ever storing an address or port in Prometheus.

```
netflow_flow_bytes_total{device="edge-01", site="branch-01", protocol="tcp",
                         direction="ingress", application="erp", criticality="critical"}
```

## Prerequisites

- Flows already arrive and carry `device`/`site`/`role` — see
  [docs/devices/](../devices/) (and [flow-behind-nat.md](flow-behind-nat.md) for NAT'd exporters).
- Hosts, applications and services modelled in Infrahub —
  [model-applications.md](model-applications.md). A service needs its host's
  `address`, `protocol` and `port`.

## Turn it on

```sh
make seed      # services in source-of-truth/devices/*.yml
make render    # writes the lookup tables and the processors
```

Telegraf reloads within 30 s. Nothing else to configure — with no services
modelled, `make render` writes no processors and flows look exactly as before.

## How a flow is matched

`make render` writes three files to `observability/telegraf/generated/`:

| File | Holds |
|---|---|
| `services-dst.json` | `"<server ip>:<port>/<protocol>" → {application, criticality}` |
| `services-src.json` | the same keys, for replies |
| `netflow-applications.conf` | the two lookups and a merge step that apply them — **only written when at least one service exists** |

A request's server is its **destination**; the reply's server is its
**source**. Both directions are looked up, so a session's upload and download
both count toward the application. A flow matching no service — internet
traffic, unmodelled servers — gets `application="other"`, `criticality="none"`.

The lookups run on the flow record while it still has addresses and ports,
then `observability/telegraf/conf.d/netflow.conf` drops them: only the bounded labels reach
Prometheus. Series per device grow with the number of **applications**, not
hosts or ports.

**Why the processors are generated, not in `conf.d/`:** Telegraf reloads
`conf.d/` the moment it changes (a `git pull`), and `processors.lookup`
refuses to start on a missing file. Generating the processors next to their
tables means the config never names a table that does not exist yet.

## Reading the numbers

`netflow_flow_bytes_total` is a **gauge**: bytes per 60 s aggregation window
(the `_total` is historical, not a counter).

| Want | PromQL |
|---|---|
| bit/s by application | `sum by (application) (avg_over_time(netflow_flow_bytes_total[5m])) * 8 / 60` |
| bytes in the last hour | `sum by (application) (avg_over_time(netflow_flow_bytes_total[1h])) * 60` |
| unclassified share | `sum(netflow_flow_bytes_total{application="other"}) / sum(netflow_flow_bytes_total)` |

**Every exporter on a path reports the same flow.** Summing a branch router
and the core router it tunnels to counts a session twice. For totals, filter
to one device per path (e.g. `device=~"branch-.*"`).

## Where it shows up

| Where | What |
|---|---|
| Grafana → DarqCube → **Applications** | throughput by application and criticality, unclassified share, which devices carry each application |
| Alert **CriticalApplicationSilent** | a `critical` application that carried traffic in the last 6 h has had none for 15 min |
| MCP `get_flow_summary` (mcp-prometheus) | `by_application` next to `by_protocol` |

## Verify

```sh
cat observability/telegraf/generated/services-dst.json | head
docker compose logs --since 2m telegraf | grep -i lookup      # no errors
curl -s localhost:${PROMETHEUS_PORT}/api/v1/query \
  --data-urlencode 'query=count by (application) (netflow_flow_bytes_total)'
```

Allow two minutes after `make render`: NetFlow v9 records decode only after
the exporter's next template refresh, and the collector sums over 60 s.

| Symptom | Cause |
|---|---|
| everything is `other` | the service's host `address` is not the address the flows carry (NAT, a VIP, a second interface), or port/protocol differ |
| no `application` label at all | no services modelled — `make render` printed no `flow  apps` line |
| one application much larger on some devices | summing several exporters on one path — see above |
