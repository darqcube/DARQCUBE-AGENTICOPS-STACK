# MikroTik RouterOS

Commands below target **RouterOS 7**. Most work on v6; `/system ntp client` and
some `print` output formats differ.

## 1. Identity and SSH

```
/system identity set name=mt-01
```

The identity **must equal** the `name` in `devices.yml` — RouterOS sends it as
the syslog hostname, and that is the identity join.

```
/user add name=darqcube group=full password=<PASSWORD>
/ip service set ssh disabled=no
/ip service set ssh port=22
```

`group=full` is needed for configuration changes. For read-only monitoring,
`group=read` is enough — but `make config-put` will then fail.

## 2. SNMPv3

```
/snmp community add name=darqcube security=private \
    authentication-protocol=SHA1 authentication-password=<AUTH_PASS> \
    encryption-protocol=AES encryption-password=<PRIV_PASS>

/snmp set enabled=yes contact="DarqCube" location=hq trap-version=3
```

RouterOS calls the SNMPv3 user a "community" — the name is misleading, it is the
USM user. Must match `SNMPV3_USER`, `SNMPV3_AUTH`, `SNMPV3_PRIV` in `.env`.

### What is collected

| Metric | OID | MIB |
|---|---|---|
| `cpu_usage` | `1.3.6.1.2.1.25.3.3.1.2` | HOST-RESOURCES-MIB (`hrProcessorLoad`) |
| `memory_used` / `memory_total` | `1.3.6.1.2.1.25.2.3.1.6` / `.5` | HOST-RESOURCES-MIB (`hrStorage`) |
| `device_uptime` | `1.3.6.1.2.1.1.3.0` | SNMPv2-MIB |
| interface status, counters, errors, speed | IF-MIB | standard |

> **RouterOS has no vendor CPU MIB.** MIKROTIK-MIB (enterprise 14988) carries
> health, voltage and wireless — not CPU. Both values come from the standard
> HOST-RESOURCES-MIB instead, which is why this platform's `platforms.yml` entry
> looks different from the other two.
>
> `hrProcessorLoad` is a table with one row per core, and `hrStorage` indexes
> every storage type — RAM, disk, and more. **Confirm which index is main memory
> on one box before trusting the numbers**; it varies by build. Check with:
> ```
> /system resource print
> ```

## 3. Syslog

```
/system logging action add name=darqcube target=remote \
    remote=<SYSLOG_COLLECTOR_IP> remote-port=1514 src-address=<mgmt-ip>

/system logging add topics=info action=darqcube
/system logging add topics=warning action=darqcube
/system logging add topics=error action=darqcube
/system logging add topics=critical action=darqcube
```

`remote-port` must match `SYSLOG_PORT` in `.env`.

The RouterOS format is
`<134>Sep 14 10:23:45 mt-01 system,info: <text>` — identity as the hostname and
a comma-separated `topics` list where Cisco puts `%FACILITY-SEV-MNEMONIC`. There
is **no severity in the message body**, so the stack derives it from the syslog
PRI. The Logstash pattern handles this.

Add `topics=debug` only temporarily — RouterOS debug logging is very chatty.

## 4. Traffic Flow (NetFlow)

```
/ip traffic-flow set enabled=yes interfaces=all \
    active-flow-timeout=1m inactive-flow-timeout=15s

/ip traffic-flow target add dst-address=<SYSLOG_COLLECTOR_IP> port=12055 version=9
```

`port` must match `NETFLOW_PORT`. RouterOS supports v5, v9 and IPFIX; use v9
against port 12055, or `version=ipfix` against 14739.

```yaml
    flow_enabled: true
```

## 5. NTP

```
/system ntp client set enabled=yes
/system ntp client servers add address=<NTP_IP>
/system clock set time-zone-name=UTC
```

On RouterOS 6 it is a single command:
`/system ntp client set enabled=yes primary-ntp=<NTP_IP>`

## 6. One-shot block

```
/system identity set name=mt-01
/user add name=darqcube group=full password=<PASSWORD>
/ip service set ssh disabled=no
/snmp community add name=darqcube security=private \
    authentication-protocol=SHA1 authentication-password=<AUTH_PASS> \
    encryption-protocol=AES encryption-password=<PRIV_PASS>
/snmp set enabled=yes contact="DarqCube" location=hq
/system logging action add name=darqcube target=remote \
    remote=<SYSLOG_COLLECTOR_IP> remote-port=1514 src-address=<mgmt-ip>
/system logging add topics=info action=darqcube
/system logging add topics=warning action=darqcube
/system logging add topics=error action=darqcube
/ip traffic-flow set enabled=yes interfaces=all
/ip traffic-flow target add dst-address=<SYSLOG_COLLECTOR_IP> port=12055 version=9
/system ntp client set enabled=yes
/system ntp client servers add address=<NTP_IP>
```

## 7. Add it to the stack

```yaml
  - name: mt-01
    site: branch-01
    role: wan
    platform: routeros
    management_ip: 10.0.0.31
    flow_enabled: true
```

```bash
make seed && make render
```

## 8. Verify

```bash
source .env
docker compose exec telegraf nc -zvu 10.0.0.31 161
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=cpu_usage{device=\"mt-01\"}"
curl -sG "localhost:${LOKI_PORT}/loki/api/v1/query_range" --data-urlencode 'query={device="mt-01"}'
make state DEV=mt-01
```

## Parsing RouterOS output

`make state` runs `/interface print terse without-paging`. The **terse** form is
deliberate: it is the machine-readable one, and `ntc-templates` has no template
for a plain `/interface print`.

RouterOS output is columnar with flag letters in a leading column (`0 R`,
`1 X`), unlike the key-value and fixed-header formats most templates target, so
coverage is thinner than for Cisco. Expect to write a template for any command
beyond the basics — [../how-to/add-a-textfsm-template.md](../how-to/add-a-textfsm-template.md).

## Assurance works the same as every other platform

RouterOS has no gNMI, so metrics come from SNMP. Everything else takes the same
path as Cisco and Huawei: Netmiko gets the text, TextFSM parses it, and
`automation/assurance/normalise.py` maps RouterOS's flag letters to the same
shape the other vendors produce.

```bash
make check DEV=mt-01        # the same rules that run against Cisco
make snapshot DEV=mt-01     # comparable state, for pre/post comparison
```

> RouterOS encodes interface state in single-letter flags — `R` running, `X`
> disabled, `D` dynamic — where other vendors use columns. ntc-templates puts
> those flags in the `status` field, **not** one called `flags`. The normaliser
> reads `status`; reading the wrong key reported every running interface as
> down, which is exactly the kind of silently-wrong result `_require()` now
> prevents.
