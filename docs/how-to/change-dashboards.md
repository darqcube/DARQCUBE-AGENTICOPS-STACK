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
