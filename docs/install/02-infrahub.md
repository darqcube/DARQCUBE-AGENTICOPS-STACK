# Infrahub — the source of truth

## What it does here

Holds what **should** exist: devices, sites, roles, platforms, management IPs.
Every collector is labelled from it, which is why a metric and a log line for
the same device carry the same `device`, `site` and `role`.

Without it: collection continues for devices already rendered, but you cannot
add or change a device, `make render` fails, and the automation layer has no
inventory.

## Containers

Seven, and that count is Infrahub's own architecture rather than a choice:

| Container | Role |
|---|---|
| `infrahub-server` | API and UI |
| `infrahub-worker` | Prefect worker for background tasks |
| `neo4j` | the graph store |
| `redis` | cache |
| `rabbitmq` | message bus |
| `task-db` | Postgres, for the embedded Prefect |
| `task-manager` | Prefect API — runs the *Infrahub* image, not a Prefect image |

Only `infrahub-server` is published.

## Configuration

| Where | What |
|---|---|
| `source-of-truth/schema/darqcube.yml` | the data model |
| `source-of-truth/devices/sites.yml` | your sites |
| `source-of-truth/devices/devices.yml` | your devices |
| `.env` → `INFRAHUB_PORT` | published port (default 18000) |
| `.env` → `INFRAHUB_ADMIN_TOKEN` | API token, also the initial admin token |
| `.env` → `NEO4J_PASSWORD`, `RABBITMQ_PASSWORD`, `POSTGRES_PASSWORD`, `INFRAHUB_SECRET_KEY` | backing services |
| `.env` → `INFRAHUB_BRANCH` | which branch is read (default `main`) |

## How to change it

Add a device → [../how-to/add-a-device.md](../how-to/add-a-device.md).
Add a vendor → [../how-to/add-a-platform.md](../how-to/add-a-platform.md).

```bash
make schema    # load the schema after editing darqcube.yml
make seed      # apply devices.yml / sites.yml  (idempotent — also updates)
make render    # push to the collectors
```

## Verify

```bash
source .env
curl -s "localhost:${INFRAHUB_PORT}/graphql" \
  -H "X-INFRAHUB-KEY: $INFRAHUB_ADMIN_TOKEN" \
  -d '{"query":"{NetworkDevice{edges{node{name{value} platform{value}}}}}"}'
```

UI: `http://<vm-ip>:${INFRAHUB_PORT}`, login `admin` / your admin token.

## Problems

| Symptom | Cause |
|---|---|
| Schema load fails with `string_too_long`, no field named | a `description:` is ≥128 characters. Infrahub rejects the **whole** file and does not say which. `make test` checks this. |
| `task-db` will not start | Postgres 18+ must mount at `/var/lib/postgresql`, **not** `/var/lib/postgresql/data` |
| `NodeNotFoundError` on `site.peer` | an SDK query missing `prefetch_relationships=True` |
| Slow first start | normal — Neo4j takes 1–2 minutes. `make up` waits. |
| Device seeded but not polled | `make render` was not run |
