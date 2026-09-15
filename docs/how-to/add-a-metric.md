# Add a metric from a new OID

Where the change goes depends on whether the OID is the same on every vendor.

## Same on all platforms → the shared template

IF-MIB, SNMPv2-MIB, HOST-RESOURCES-MIB and anything else standard belongs in
`observability/telegraf/profiles/_interfaces.conf.tmpl`, which is collected once
for the whole fleet:

```toml
  [[inputs.snmp.table]]
    name = "interface"
    ...
    [[inputs.snmp.table.field]]
      name = "in_discards"
      oid = "1.3.6.1.2.1.2.2.1.13"          # ifInDiscards
```

## Vendor-specific → platforms.yml

Enterprise MIBs differ per vendor, so they belong in `platforms.yml`:

```yaml
ios_xe:
  snmp:
    temperature:
      oid: 1.3.6.1.4.1.9.9.13.1.3.1.3       # ciscoEnvMonTemperatureValue
      mib: CISCO-ENVMON-MIB
```

Then teach the renderer to emit it — `source-of-truth/scripts/render-inventory.py`,
in `resource_tables()`. Follow the pattern the `cpu` table already uses.

## The metric name is `<measurement>_<field>`

This is the single thing to get right. Prometheus sees the Telegraf measurement
and field joined with an underscore:

| Measurement | Field | Metric in Prometheus |
|---|---|---|
| `interface` | `oper_status` | `interface_oper_status` |
| `cpu` | `usage` | `cpu_usage` |
| `memory` | `used` | `memory_used` |
| `device` (`name_override`) | `uptime` | `device_uptime` |

So to get `interface_in_discards`, the field must be named `in_discards` inside
the table named `interface` — not `interface_in_discards`, which would produce
`interface_interface_in_discards`.

## Use numeric OIDs

```toml
oid = "1.3.6.1.2.1.2.2.1.13"     # correct
oid = "IF-MIB::ifInDiscards"     # FAILS
```

The Telegraf image ships no MIB files. A symbolic name fails at startup with
`translating: MIB search path: ...` and the whole input refuses to load.

## Tag or field?

```toml
is_tag = true      # becomes a Prometheus label — must be LOW cardinality
```

Names, types and descriptions are tags. Anything that changes every poll — a
counter, a percentage, a timestamp — must be a field. A high-cardinality tag
creates a new time series per value and will fill the TSDB.

## Apply and verify

```bash
make render
docker compose restart telegraf            # only needed if you edited conf.d/
sleep 40
curl -s "localhost:${PROMETHEUS_PORT:-9090}/api/v1/query?query=interface_in_discards"
```

If it returns an empty result, check Telegraf actually loaded the config:

```bash
docker compose logs telegraf | grep -iE 'error|loaded inputs'
```

A device that does not implement the OID returns nothing for it — that is an
empty field, never an error, so silence here means "check the OID on the
device", not "the config is broken".
