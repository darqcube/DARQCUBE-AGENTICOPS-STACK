# Grafana and Alertmanager

## Grafana

Dashboards over Prometheus and Loki. The only user-facing component.

Without it: collection continues and alerts still fire — you just cannot see
graphs.

### Configuration

| Where | What |
|---|---|
| `observability/grafana/provisioning/datasources/datasources.yml` | Prometheus and Loki |
| `observability/grafana/provisioning/dashboards/provider.yml` | where dashboards load from |
| `observability/grafana/provisioning/dashboards/darqcube/` | the dashboards |
| `.env` → `GRAFANA_ADMIN_USER` / `_PASSWORD` | login |
| `.env` → `GRAFANA_PORT` | published port (default 13000) |

### Dashboards are read-only in the UI

`allowUiUpdates: false`, deliberately: what is in the repo is what is deployed.
To change one, edit it in the UI, copy the JSON model, and paste it over the
file — [../how-to/change-dashboards.md](../how-to/change-dashboards.md).

### Verify

```bash
source .env
curl -s "localhost:${GRAFANA_PORT}/api/health"
curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" "localhost:${GRAFANA_PORT}/api/datasources" \
  | .venv/bin/python -c "import json,sys;[print(d['name'],d['uid']) for d in json.load(sys.stdin)]"
```

Datasource *health* is the check that matters — a datasource can exist and not
connect:

```bash
curl -s -u "admin:${GRAFANA_ADMIN_PASSWORD}" \
  "localhost:${GRAFANA_PORT}/api/datasources/uid/darqcube-prometheus/health"
```

---

## Alertmanager

Receives alerts from Prometheus, groups and deduplicates them, delivers to one
webhook.

Without it: alerts still *evaluate* in Prometheus and are visible there — they
just are not delivered anywhere.

### Configuration

| Where | What |
|---|---|
| `observability/alertmanager/alertmanager.yml.tmpl` | **the template — edit this** |
| `.env` → `ALERT_WEBHOOK_URL` | where alerts go |
| `.env` → `ALERTMANAGER_PORT` | published port (default 19093) |

### Why it is a template

**Alertmanager has no environment-variable expansion at all** — a `${VAR}` in
its config is a hard parse failure. The `config-init` service substitutes
`__ALERT_WEBHOOK_URL__` into a volume before Alertmanager starts.

Edit the `.tmpl`, never the rendered copy, then:

```bash
docker compose up -d --force-recreate config-init alertmanager
```

### Grouping and inhibition

Alerts group by `alertname` and `device`. One inhibit rule matters: a
`DeviceUnreachable` alert suppresses `InterfaceDown` for the same device — you
get the cause, not a hundred symptoms.

### Verify

```bash
source .env
curl -s "localhost:${ALERTMANAGER_PORT}/-/ready"
curl -s "localhost:${ALERTMANAGER_PORT}/api/v2/alerts"
docker compose logs config-init          # says where alerts will be delivered
```

### Problems

| Symptom | Cause |
|---|---|
| Will not start, config parse error | a `${VAR}` left in the template — it is not expanded |
| Alerts fire but arrive nowhere | `ALERT_WEBHOOK_URL` empty; the default is a local no-op |
| Changed the URL, no effect | recreate `config-init`, not just `alertmanager` |
