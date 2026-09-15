# Add a device

## 1. Configure the device itself

Follow the guide for its platform — SSH user, SNMPv3, syslog destination, flow
export:

- [Cisco IOS-XE](../devices/cisco-ios-xe.md)
- [Huawei VRP](../devices/huawei-vrp.md)
- [MikroTik RouterOS](../devices/mikrotik-routeros.md)

**The device's hostname must exactly equal the name you use in step 2.** It is
the key that joins a syslog line to a metric to an Infrahub record. If they
differ, logs arrive labelled `unknown` and nothing correlates — and nothing
reports the mismatch.

## 2. Add it to the source of truth

Edit `source-of-truth/devices/devices.yml`:

```yaml
devices:
  - name: cr2                      # == the device's hostname / sysname / identity
    site: hq                       # must exist in sites.yml
    role: core                     # core | distribution | access | wan | edge | firewall
    platform: ios_xe               # ios_xe | vrp | routeros  (see platforms.yml)
    management_ip: 10.0.0.12
    telemetry_mode: snmp           # snmp (default) | gnmi — gnmi is ios_xe only
    flow_enabled: true             # does it export NetFlow/IPFIX?
    description: Second core router
```

Adding a site first, if you need one — `source-of-truth/devices/sites.yml`:

```yaml
sites:
  - name: branch-02
    description: Branch site 2
```

## 3. Apply it

```bash
make seed      # devices.yml -> Infrahub
make render    # Infrahub -> Telegraf and Logstash
```

Both are idempotent, so re-running is always safe. `seed` also *updates*
existing devices, so this is how you change a device, not just add one.

Telegraf picks up the new config within 30 seconds (`--watch-config poll`).
Logstash re-reads the device table within 60 seconds. No restart needed.

## 4. Check it worked

```bash
# In the source of truth
curl -s localhost:${INFRAHUB_PORT:-8000}/graphql \
  -H "X-INFRAHUB-KEY: $INFRAHUB_ADMIN_TOKEN" \
  -d '{"query":"{NetworkDevice(name__value:\"cr2\"){edges{node{name{value}}}}}"}'

# Being polled (allow one SNMP interval)
curl -s "localhost:${PROMETHEUS_PORT:-9090}/api/v1/query?query=device_uptime{device=\"cr2\"}"

# Sending logs (generate one on the device first)
curl -sG localhost:${LOKI_PORT:-3100}/loki/api/v1/query_range \
  --data-urlencode 'query={device="cr2"}'

# Reachable for automation
make state DEV=cr2
```

If metrics do not appear, see [troubleshooting.md](troubleshooting.md#a-device-is-not-appearing-in-prometheus).

## Removing a device

Set its status rather than deleting it, so history is kept:

```yaml
  - name: cr2
    status: decommissioned      # active | provisioning | maintenance | decommissioned
```

Then `make seed && make render`. Only `active` devices are rendered into the
collectors, so polling stops while the record and its past metrics remain.

Use `maintenance` to stop alerting on a device you are working on.
