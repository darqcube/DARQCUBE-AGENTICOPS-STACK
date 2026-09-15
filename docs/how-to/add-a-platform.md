# Support a new vendor or platform

Five edits. Miss one and the platform is **half-supported**, which fails
silently — devices seed and appear healthy while one feed quietly collects
nothing.

## 1. `platforms.yml`

```yaml
nx_os:
  display_name: Cisco NX-OS
  vendor: cisco
  netmiko_type: cisco_nxos          # how Netmiko CONNECTS
  textfsm_platform: cisco_nxos      # how ntc-templates NAMES its templates
  gnmi: true
  gnmi_port: 50051
  show_run: show running-config
  state_cmd: show interface brief
  onboarding_doc: docs/devices/cisco-nx-os.md
  snmp:
    cpu:
      oid: 1.3.6.1.4.1.9.9.305.1.1.1.0
      mib: CISCO-SYSTEM-EXT-MIB
    memory:
      kind: used_free               # percent | used_free | used_total
      used: 1.3.6.1.4.1.9.9.48.1.1.1.5
      free: 1.3.6.1.4.1.9.9.48.1.1.1.6
      mib: CISCO-MEMORY-POOL-MIB
```

### `netmiko_type` and `textfsm_platform` are not always the same

Netmiko connects to IOS-XE as `cisco_xe`, but ntc-templates files every
IOS/IOS-XE template under `cisco_ios` and has **zero** named `cisco_xe`. Using
one value for both silently breaks every parse. Check before you trust it:

```bash
.venv/bin/python -c "
import ntc_templates, os
d = os.path.join(os.path.dirname(ntc_templates.__file__), 'templates')
rows = open(os.path.join(d,'index')).read().splitlines()
print([l for l in rows if ', cisco_nxos,' in l][:10])"
```

### `memory.kind` is load-bearing

Vendors disagree about what "memory used" means — Huawei reports a percentage,
Cisco bytes, MikroTik allocation units. `kind` tells the Prometheus recording
rule how to convert, so `memory_used_percent` is comparable across the fleet.
Get this wrong and a dashboard shows 847,000,000%.

## 2. The Infrahub schema

`source-of-truth/schema/darqcube.yml`, in `NetworkDevice.platform`:

```yaml
        choices:
          - name: ios_xe
          - name: nx_os
            label: Cisco NX-OS
```

Keep every `description:` under 128 characters — Infrahub rejects the entire
schema load with `string_too_long` and never names the offending field.

```bash
make schema
```

A test asserts the schema choices exactly match the `platforms.yml` keys, so a
mismatch fails `make test` rather than in production.

## 3. A syslog grok pattern

See [add-a-syslog-format.md](add-a-syslog-format.md).

## 4. A TextFSM template, if `ntc-templates` has none

See [add-a-textfsm-template.md](add-a-textfsm-template.md). Even when one
exists, add a sample so the coverage is verified:
`automation/textfsm/samples/manifest.yml`.

## 5. An onboarding document

`docs/devices/<platform>.md` — the device-side config: SSH user, SNMPv3,
syslog destination, flow export, NTP, and the rule that the device hostname
must equal its Infrahub name. Copy the shape of an existing one.

## Verify

```bash
make test                      # schema/platform parity, template coverage
make seed && make render
ls observability/telegraf/generated/     # expect snmp-nx_os.conf
make state DEV=<a device of the new platform>
```

## 6. An assurance normaliser

`automation/assurance/normalise.py` — this is what lets one set of rules cover
every vendor. Add a function that maps the platform's parsed rows to the common
shape:

```python
def _nxos(row: dict) -> dict:
    _require(row, "interface", "admin_state", "link_status")
    return {
        "interface": row["interface"],
        "admin_up": row["admin_state"].lower() == "up",
        "oper_up": row["link_status"].lower() == "up",
    }

_MAP = {"ios_xe": _cisco, "vrp": _vrp, "routeros": _routeros, "nx_os": _nxos}
```

**Call `_require()` with the fields you read.** A normaliser that reads a field
the parser does not emit produces plausible but *wrong* booleans — which is far
worse than an error, because the result still looks like data. This is a real
bug that happened here: RouterOS flags live in `status`, not `flags`.

A test asserts every platform in `platforms.yml` has a normaliser.
