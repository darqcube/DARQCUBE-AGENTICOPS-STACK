# Add a service to the stack

Decide which functional group it belongs to, then edit that group's compose
file. Services are grouped by what they do, not by vendor or image.

| Group | File | For |
|---|---|---|
| source-of-truth | `compose/source-of-truth.yaml` | intent, inventory |
| observability | `compose/observability.yaml` | collection, storage, dashboards |
| automation | `compose/automation.yaml` | anything that touches a device |
| mcp | `compose/mcp.yaml` | tool surfaces for an AI |

If it fits none of them, that is worth a moment's thought before adding a fifth.

## The template

```yaml
  my-service:
    image: vendor/image:1.2.3            # pin a version, never :latest
    labels:
      com.darqcube.group: observability  # required — this is how grouping works
      com.darqcube.role: collector       # collector | store | ui | api | mcp | backend
    profiles: [devices]                  # optional: only starts with this profile
    environment:
      SOME_SETTING: ${SOME_SETTING:-default}
    volumes:
      - ../observability/my-service:/etc/my-service:ro
      - my-service-data:/var/lib/my-service
    ports:
      - "${MY_SERVICE_PORT:-8080}:8080"  # host side configurable, container side fixed
    healthcheck:                         # REQUIRED — see below
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8080/health"]
      interval: 15s
      timeout: 5s
      retries: 10
      start_period: 20s
    networks: [darqcube]
    restart: unless-stopped

volumes:
  my-service-data:
```

## A healthcheck is not optional

`make up` runs `docker compose up -d --wait`, which blocks until every container
is healthy. A service without a healthcheck is treated as ready the moment it is
created, and every test that follows races its startup.

If the image has no HTTP endpoint and no `curl`:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "nc -z localhost 8080 || exit 1"]
```

## Labels are the grouping mechanism

```bash
docker ps --filter "label=com.darqcube.group=observability"
```

A service without `com.darqcube.group` is invisible to that, and to the tests
that check group membership.

## Configuration

Config files go under the group's directory — `observability/my-service/` — and
are bind-mounted read-only. Anything that varies per install goes in `.env` with
a default:

```yaml
      SOME_SETTING: ${SOME_SETTING:-default}      # optional
      REQUIRED_SECRET: ${REQUIRED_SECRET:?set in .env}   # fails fast if missing
```

The `:?` form is worth using for secrets — `docker compose config` fails with a
clear message instead of the container starting with an empty password.

**Not every tool expands environment variables in its own config file.**
Prometheus and Alertmanager do not; Loki needs an explicit flag. Check before
relying on it — Prometheus will silently store `${VAR}` as a literal string.

## Building your own image

```yaml
    build:
      context: ../observability/my-service
    image: darqcube/my-service:local
```

Keep the Dockerfile beside the source it builds, inside its group's directory.

## Verify

```bash
docker compose config -q                  # merges cleanly
docker compose up -d my-service
docker compose ps my-service              # reaches "healthy"
docker ps --filter "label=com.darqcube.group=observability"
```

Then add it to `docs/install/` and to the failure table in
`docs/architecture.md`, so the next person knows what breaks without it.
