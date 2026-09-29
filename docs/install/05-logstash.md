# Logstash — syslog

## What it does here

Owns the syslog path end to end: receives on UDP and TCP (same port), parses three vendor formats,
enriches each line with `device`/`site`/`role` from the Infrahub-rendered table,
and ships to Loki.

It does **not** handle metrics or flow — that is Telegraf.

Without it: all log ingest stops. Metrics and automation are unaffected.

## A custom image

`logstash-output-loki` is not bundled with Logstash, and it is the entire reason
Logstash is here. `observability/logstash/Dockerfile` installs it.

```bash
docker compose build logstash
```

## Configuration

| Where | What |
|---|---|
| `observability/logstash/pipeline/syslog.conf` | the pipeline |
| `observability/logstash/patterns/network.grok` | per-vendor grok patterns |
| `observability/logstash/samples/syslog-samples.txt` | test fixtures |
| `.env` → `SYSLOG_PORT` | listening port (default 1514) |

## The pipeline, in six stages

1. **Input** — UDP and TCP on 514 inside the container. TCP is read one
   message per line, which is how IOS frames it; a device switches transport
   with no stack change.
2. **Parse** — vendor patterns in order, catch-all last.
3. **Severity** — from the message body where the vendor provides it (Cisco,
   Huawei), otherwise derived from the PRI (MikroTik).
4. **Enrich** — `translate` against `generated/devices.yml`, matching on
   hostname with a source-IP fallback. Re-read every 60s, so `make render`
   propagates without a restart.
5. **Line** — rebuilds the stored log line in vendor notation, mnemonic
   included: `%SYS-5-CONFIG_I: …`, `IFNET/4/LINK_STATE: …`, `system,info: …`.
   Loki keeps only this line plus the labels; anything in neither is gone.
6. **Output** — to Loki, with a fixed label list.

The pipeline is not reloaded automatically — after changing it,
`make restart SVC=logstash`.

## Unparseable lines are kept

A line nothing matches is still delivered, tagged `_grokparsefailure`, with
`device="unparsed"`. A log you cannot parse is worth far more than one you threw
away — and it is how you find the format you need to add.

```logql
{device="unparsed"}
```

## How to change it

Add a vendor format → [../how-to/add-a-syslog-format.md](../how-to/add-a-syslog-format.md).

```bash
docker compose restart logstash
```

## Verify

```bash
# Syntax only
docker compose exec logstash logstash --config.test_and_exit

# The real test: parse the committed samples through the real filter block
.venv/bin/python -m pytest automation/tests/test_syslog_parsing.py -v

# End to end
logger -n <vm-ip> -P ${SYSLOG_PORT} -p local7.info "test from $(hostname)"
curl -sSG localhost:${LOKI_PORT}/loki/api/v1/query_range --data-urlencode 'query={device="unparsed"}'
```

## Problems

| Symptom | Cause |
|---|---|
| Everything tagged `_grokparsefailure` | the vendor's format has no pattern |
| Logs arrive labelled `unknown` | the device's hostname ≠ its Infrahub `name` |
| Logs arrive, but `\|= "CONFIG_I"` finds nothing | the stored line lacks the mnemonic — stage 5 missing, or the output's `message_field` points at `msg` |
| A device's logs never arrive | its management interface is in a VRF and the logging host does not name it, so syslog leaves through the global table |
| Some lines of a burst missing, no error anywhere | UDP loss on the path — switch the device to TCP. To find where: send a counted burst from the VM and from the device's network, and compare the Logstash container's `Udp: InDatagrams` before and after |
| A vendor silently falls through | grok word boundaries. `NONNEGINT` is `\b[0-9]+\b` and `WORD` is `\b\w+\b`; in Huawei's `%%01IFNET` there is no boundary between `1` and `I`, so neither matches. Use explicit classes like `(?<x>[0-9]+)`. |
| No logs at all | check the device points at the VM's routable IP and the right port |
| Slow start | normal — the JVM takes ~60s |
