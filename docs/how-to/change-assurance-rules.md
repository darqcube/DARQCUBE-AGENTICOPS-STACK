# Add or change an assurance check

Rules live in `automation/assurance/rules.yml`. Each one names its **source**:

| `source:` | Data | Platforms |
|---|---|---|
| `interfaces` (default) | TextFSM rows, normalised to one shape | every platform |
| `pyats` | Genie `learn()` / `parse()` output | only those with a `pyats:` block declaring the feature |

A `pyats` rule on a platform that cannot support it comes back **`skipped`**
with the reason — never `pass`, never `fail`.

```bash
make check DEV=cr1
```

## Add a rule

```yaml
rules:
  - name: uplinks_must_be_up
    description: A port described as an uplink is load-bearing and must be up.
    severity: error              # error | warning
    applies_to: all              # all, or a list: [ios_xe, vrp]
    check: admin_up_means_oper_up
    ignore: '^(Loopback|Null|Vlan|lo|bridge)'
```

No restart needed — `rules.yml` is read on each run, and the automation
directory is bind-mounted.

## The checks available

| `check:` | `source:` | Does |
|---|---|---|
| `admin_up_means_oper_up` | interfaces | flags interfaces the operator enabled that are not passing traffic |
| `min_interfaces` | interfaces | fails if fewer than `minimum:` interfaces were found — usually a parsing failure, not a device with no ports |
| `bgp_peers_established` | pyats | fails for every BGP session not `Established`. Needs `feature: bgp` |

## A pyATS rule

```yaml
  - name: bgp_peers_established
    description: Every BGP peer should be Established.
    severity: error
    source: pyats
    feature: bgp                     # must match a key under `pyats:` in platforms.yml
    check: bgp_peers_established
    ignore_peers: [192.0.2.1]        # a bare address (any VRF), or "VRF1 192.0.2.1"
```

Which platforms can run it is decided by `platforms.yml`, not by the rule:

| Platform | `feature: bgp` from |
|---|---|
| Cisco IOS-XE | `learn: [bgp]` |
| Huawei VRP | `parse: {bgp: display bgp peer}` |
| MikroTik | not supported — the rule is skipped |

BGP sessions are identified by **VRF, address family and peer** — failures read
`VRF1 2.2.2.2` or `default/ipv4 5.5.5.5`. The same peer address routinely has a
separate session in several VRFs, and collapsing them would let one being down
hide behind another being up.

A new pyATS check is a function in `automation/pyats/checks.py`, registered in
its `CHECKS`. It receives the Genie data for the rule's feature. Test it against
the golden fixtures in `automation/pyats/samples/` — they are Genie's own test
data, so the shape is real.

## Add a new kind of check

Rules are declarative, but a *check* is a function. Add it in
`automation/assurance/engine.py`:

```python
def _check_description_present(interfaces: list[dict], rule: dict) -> list[dict]:
    """Every physical port should say what it connects to."""
    return [
        {"interface": i["interface"], "detail": "no description"}
        for i in interfaces
        if not i["raw"].get("description")
    ]

CHECKS = {
    "admin_up_means_oper_up": _check_admin_up_means_oper_up,
    "min_interfaces": _check_min_interfaces,
    "description_present": _check_description_present,
}
```

A check takes the normalised interfaces and the rule, and returns a list of
failures (empty means pass). A test asserts every `check:` named in `rules.yml`
exists in `CHECKS`.

## What "normalised" means

Each interface reaches a check in the same shape, whatever the vendor:

```python
{"interface": "GigabitEthernet1", "admin_up": True, "oper_up": True, "raw": {...}}
```

`raw` is the original parsed row, if a check needs something vendor-specific.

The mapping lives in `automation/assurance/normalise.py`, and it exists because
the vendors agree on nothing except the concept:

| Vendor | Admin state | Oper state |
|---|---|---|
| Cisco | `status` — `"administratively down"` | `proto` — `up`/`down` |
| Huawei | `phy` — leading `*` means admin-down | `protocol` — `up`/`down` |
| MikroTik | `status` flags — `X` = disabled | `status` flags — `R` = running |

Without this layer every rule would need a branch per vendor, and adding a
platform would mean revisiting every rule.

## Adding a platform

A new platform needs a normaliser function, or its rules cannot run. See
[add-a-platform.md](add-a-platform.md). A test asserts every platform in
`platforms.yml` has one.

## Comparing before and after a change

```bash
make snapshot DEV=cr1 > /tmp/before.json
make config-put DEV=cr1 FILE=change.txt      # reports what changed
make snapshot DEV=cr1 > /tmp/after.json
```

`config-put` takes its own snapshot on both sides and reports the difference,
so a push says what it actually did rather than only that it completed:

```json
"state_change": {
  "changed": true,
  "changes": [{"what": "GigabitEthernet1 oper_up", "from": true, "to": false}]
}
```

Snapshots deliberately exclude counters and timers — those change every poll,
and including them would make every comparison noise.

## Three things that raise rather than passing quietly

| Situation | Why it must fail |
|---|---|
| TextFSM parses nothing | `[]` is indistinguishable from "no interfaces" |
| A normaliser reads a field the parser does not emit | produces plausible but **wrong** booleans — worse than an error, because it still looks like data |
| Comparing against an empty snapshot | an empty side diffs clean against anything, reporting "no change" when nothing was checked |
