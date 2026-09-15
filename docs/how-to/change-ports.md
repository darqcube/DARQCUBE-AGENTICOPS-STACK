# Change a published port

Every host port is set in `.env`. Only the **host** side changes — inside the
Docker network every service still listens on its standard port, so nothing
else in the stack needs updating.

## Change one

```bash
# .env
GRAFANA_PORT=13000                # standard: 3000
```

```bash
./scripts/preflight.sh            # confirms the new port is free
docker compose up -d grafana      # recreate just that service
```

## Revert to standard ports

Set each variable to the value in its comment, **or delete the whole
`PUBLISHED PORTS` section** — the compose files fall back to the standard port
when a variable is unset:

```yaml
ports:
  - "${GRAFANA_PORT:-3000}:3000"
```

## The full set

| Variable | Shipped | Standard |
|---|---|---|
| `GRAFANA_PORT` | 13000 | 3000 |
| `INFRAHUB_PORT` | 18000 | 8000 |
| `PROMETHEUS_PORT` | 19090 | 9090 |
| `ALERTMANAGER_PORT` | 19093 | 9093 |
| `LOKI_PORT` | 13100 | 3100 |
| `AUTOMATION_PORT` | 18100 | 8100 |
| `SYSLOG_PORT` | 1514 | 514 |
| `NETFLOW_PORT` | 12055 | 2055 |
| `IPFIX_PORT` | 14739 | 4739 |

Non-standard values ship by default so the stack can run alongside something
already using the usual ports.

## Device-facing ports are different

`SYSLOG_PORT`, `NETFLOW_PORT` and `IPFIX_PORT` are what your **devices send
to**. Changing one means changing every device:

```
! Cisco
logging host 192.168.1.50 transport udp port 1514

# MikroTik
/system logging action set darqcube remote-port=1514
/ip traffic-flow target set 0 port=12055
```

Port 514 also needs root on most Linux hosts, which is why 1514 is the shipped
default.

## MCP ports

Not published at all, by design — they are reached over the Docker network at
`http://mcp-prometheus:9002` and so on. To expose one (for an external AI
platform), see [connect-an-ai-platform.md](connect-an-ai-platform.md).
