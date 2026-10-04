# Add a dashboard or a panel

Dashboards are provisioned from
`observability/grafana/provisioning/dashboards/darqcube/`. They are
**read-only in the UI** (`allowUiUpdates: false`) so that what is in the repo is
what is deployed — nobody can make an undocumented change that vanishes on the
next deploy.

## Edit an existing dashboard

The workflow that avoids hand-writing JSON:

1. Open the dashboard in Grafana and change it in the UI.
2. **Dashboard settings → JSON Model → copy.**
3. Paste over `network-overview.json`.
4. `docker compose restart grafana` (or wait 30s — the provider re-reads).

The UI will not let you *save*, which is the point. Copying the JSON is the
save.

## Shipped dashboards

All in the **DarqCube** folder, linked to each other from the top-right
*DarqCube dashboards* menu. Grafana opens on **Network Overview**
(`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH` in `compose/observability.yaml`).

| Dashboard | uid | Source | Shows |
|---|---|---|---|
| **Network Overview** | `darqcube-network` | Prometheus + Loki | the landing page: devices intended vs not reporting, site health, WAN tunnels (ifType 131), BGP sessions, WAN quality (IP SLA RTT, jitter, success, application response time), critical applications now vs normal, top applications per site, internet edge (role `internet-edge`), WAN throughput, configuration changes, routing and link events, recent logs |
| **Devices & Interfaces** | `darqcube-devices` | Prometheus (SNMP) | device table (site, role, platform, uptime), interface status, throughput in/out, top utilisation, errors, CPU and memory, **intent vs reality** (every Infrahub device: reporting or not), **BGP peers** (state, uptime, remote AS) |
| **NetFlow** | `darqcube-netflow` | Prometheus (flow) | throughput and packets by exporter, protocol, site and direction |
| **Applications** | `darqcube-applications` | Prometheus (flow) | flow traffic by application and criticality — needs services in Infrahub ([label-flows-by-application.md](label-flows-by-application.md)) |
| **Logs** | `darqcube-logs` | Loki (syslog) | volume by severity and device, top Cisco message types, errors and worse, a full log browser with a search box |

CPU and memory panels stay empty for images that do not implement the
platform's CPU/memory MIB (for example Cisco IOL); real hardware fills them.

**Loki queries in dashboards** need one matcher that cannot match an empty
value — `{device=~".+", device=~"$device"}`, not just `{device=~"$device"}`.
When a variable expands to `.*`, Loki rejects the bare form with
*queries require at least one regexp or equality matcher that does not have an
empty-compatible value*, and the panel shows nothing. A test enforces it.

## Add a new dashboard

Build it in the UI, export the JSON, then save it in that directory with:

```json
{
  "uid": "darqcube-my-dashboard",
  "title": "My Dashboard",
  "tags": ["darqcube"],
  ...
}
```

A stable `uid` matters — it is what links and `get_dashboard_link` on
mcp-grafana refer to. Remove any `"id"` field from an exported dashboard;
Grafana assigns it.

## Datasource UIDs

Reference them by uid, not by name:

```json
"datasource": { "type": "prometheus", "uid": "darqcube-prometheus" }
"datasource": { "type": "loki", "uid": "darqcube-loki" }
```

They are pinned in `observability/grafana/provisioning/datasources/datasources.yml`.

## Template variables

The stack's dashboards use `$device` and `$site`, populated from live data:

```json
{
  "name": "device",
  "type": "query",
  "datasource": { "type": "prometheus", "uid": "darqcube-prometheus" },
  "query": "label_values(device_uptime, device)",
  "includeAll": true,
  "multi": true
}
```

Use them as `{device=~"$device"}` — the regex match, because `includeAll` sets
the value to `.*`.

## Check your PromQL before deploying

```bash
printf 'groups:\n- name: t\n  rules:\n  - record: t\n    expr: %s\n' \
  'sum by (device) (rate(interface_in_octets[5m]))' > /tmp/r.yml
docker run --rm -v /tmp/r.yml:/r.yml:ro --entrypoint promtool \
  prom/prometheus:v3.4.1 check rules /r.yml
```

## Log panels

```json
{
  "type": "logs",
  "datasource": { "type": "loki", "uid": "darqcube-loki" },
  "targets": [{ "expr": "{device=~\"$device\"}" }]
}
```

Loki labels are `device`, `site`, `role`, `severity` and `platform` **only**.
Everything else — the message, Cisco mnemonics, RouterOS topics — is content,
matched with `|=`:

```logql
{device="cr1"} |= "LINK_STATE"
{site="hq", severity="error"}
```
