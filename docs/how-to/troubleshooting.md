# Troubleshooting

Start here, then jump to the section for your symptom.

> **If nothing is broken but data is missing**, the symptom is probably not in
> this page. Four failure modes in this stack produce no error at all — dropped
> UDP, a full metric buffer, repeated logins, interleaved SSH sessions. They
> have their own page: [fix-silent-data-loss.md](fix-silent-data-loss.md).

```bash
make ps                                    # what is running
docker compose logs --tail=50 <service>    # why it is not
.venv/bin/python -m pytest automation/tests -q   # what still works
```

## A device is not appearing in Prometheus

Work down the chain — each step rules out one link.

```bash
# 1. Is it in the source of truth, and active?
curl -s localhost:${INFRAHUB_PORT:-8000}/graphql \
  -H "X-INFRAHUB-KEY: $INFRAHUB_ADMIN_TOKEN" \
  -d '{"query":"{NetworkDevice(name__value:\"cr1\"){edges{node{name{value} status{value}}}}}"}'

# 2. Did it reach the collector config?   (did you run `make render`?)
grep -r cr1 observability/telegraf/generated/

# 3. Is SNMP reachable and are the credentials right?
docker compose exec telegraf nc -zvu 10.0.0.11 161
docker compose logs telegraf | grep -i 10.0.0.11

# 4. Is Prometheus scraping Telegraf at all?
curl -s "localhost:${PROMETHEUS_PORT:-9090}/api/v1/targets" \
  | .venv/bin/python -c "import json,sys; [print(t['labels']['job'], t['health']) for t in json.load(sys.stdin)['data']['activeTargets']]"
```

| Where it stopped | Cause |
|---|---|
| Not in step 1 | not seeded — `make seed` |
| In 1, not in 2 | **`make render` not run** — the most common cause by far |
| In 2, timeouts in 3 | SNMP credentials, ACL, or firewall |
| 3 fine, nothing in 4 | Telegraf failed to load config — check its logs for `E!` |

## Logs arrive but are labelled `unknown`

The device's hostname does not match its Infrahub `name`. They must be
identical — it is the key joining logs to metrics.

```bash
# What the device is actually calling itself
docker compose logs logstash | grep -i hostname | tail -5

# What Infrahub holds
grep -A2 'name:' source-of-truth/devices/devices.yml
```

Fix the device's hostname (or the Infrahub name), then `make seed && make render`.

## Logs are tagged `_grokparsefailure`

The vendor's syslog format has no matching pattern. The line is **kept**, not
dropped, so you can see it:

```logql
{device="unparsed"}
```

See [add-a-syslog-format.md](add-a-syslog-format.md).

## `make state` returns a parse error

```
template (ntc-templates) matched no lines. The device returned 14 line(s), so
this is a template problem, not an empty device.
```

Exactly what it says — the device answered and nothing could parse it. Capture
the output and write a template:
[add-a-textfsm-template.md](add-a-textfsm-template.md).

```bash
curl -s "localhost:${AUTOMATION_PORT:-8100}/device/mt-01/state?raw=1"
```

## Memory shows an absurd percentage

The platform's `memory.kind` in `platforms.yml` is wrong. Huawei reports a
percentage, Cisco bytes, MikroTik allocation units — and the recording rule
converts based on that field.

```bash
curl -s "localhost:${PROMETHEUS_PORT:-9090}/api/v1/query?query=memory_used" \
  | grep -o 'memory_kind":"[a-z_]*'
```

## No flows arriving

Flow is device-initiated, so silence is the only symptom.

```bash
# Is the listener up?
docker compose exec telegraf netstat -lnu 2>/dev/null | grep 2055

# Is anything arriving on the host?
sudo tcpdump -ni any udp port ${NETFLOW_PORT:-2055} -c 5
```

Usual causes: the device points at the wrong address (it needs the **VM's
routable IP**, not a container IP); the port does not match `NETFLOW_PORT`; or
a firewall between them.

## An alert never fires

```bash
# Is the rule loaded?
curl -s localhost:${PROMETHEUS_PORT:-9090}/api/v1/rules | grep -o '"name":"[^"]*"' | head

# Does the expression return anything right now?
curl -s --data-urlencode 'query=interface_oper_status == 2 and interface_admin_status == 1' \
  localhost:${PROMETHEUS_PORT:-9090}/api/v1/query
```

If the expression is empty, the alert is correct and there is nothing wrong. If
it returns rows but no alert fires, check the `for:` duration has elapsed.

## A container will not start

```bash
docker compose ps                    # look for Exit or unhealthy
docker compose logs <service> | tail -40
```

| Service | Common cause |
|---|---|
| `neo4j` | slowest to start — allow 2 minutes before concluding anything |
| `task-db` | Postgres 18+ must mount at `/var/lib/postgresql`, not `/data` |
| `alertmanager` | `config-init` did not run — check its logs |
| `loki` | config error; needs `-config.expand-env=true` for `${LOKI_RETENTION}` |
| `telegraf` | a bad OID or symbolic MIB name; look for `E!` |
| any | a port already in use — `./scripts/preflight.sh` |

## Everything looks healthy but nothing works

Services can all be green while nothing is connected. That is what layer 3 of
the test suite checks:

```bash
.venv/bin/python -m pytest automation/tests/test_wiring.py -v
```

## What an AI changed

Every config push is logged with the device and the lines sent:

```bash
docker compose logs automation | grep -i 'lines_sent'
ls -la automation/configs/          # pre-change archives
```

To stop an AI making changes at all, set `MCP_ALLOW_WRITE=false` and recreate
`mcp-netmiko` — the tool then does not exist.

That covers the AI. It does **not** cover anything calling the automation API
directly, which needs no credentials — see
[connect-an-ai-platform.md](connect-an-ai-platform.md#what-it-does-not-do). The
control that stops every caller is a read-only device account.

## Start over

```bash
make down                  # keeps all data
make clean                 # DELETES all volumes — asks first
```
