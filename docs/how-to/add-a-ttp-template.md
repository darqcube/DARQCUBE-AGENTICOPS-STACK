# Add a TTP template

Do this when `make config-parsed` fails:

```
no TTP template for nx_os 'interfaces'. Expected nx_os-interfaces.txt.
Available: ios_xe-interfaces.txt, routeros-interfaces.txt, vrp-interfaces.txt
```

## TTP or TextFSM?

They do different jobs, and picking the wrong one wastes an afternoon.

| | TextFSM | TTP |
|---|---|---|
| **Shape of input** | tabular — one row per line, fixed columns | hierarchical — indented blocks that contain other blocks |
| **Use for** | `show ip interface brief`, `show ip route`, ARP tables | running-config, anything with nested stanzas |
| **Lives in** | `automation/textfsm/` | `automation/ttp/` |
| **Add one** | [add-a-textfsm-template.md](add-a-textfsm-template.md) | this page |

A running-config is a tree: an interface stanza *contains* its description, its
address and its ACLs. TextFSM has no concept of a block containing other
blocks, so pointing it at a config gets you a flat list of unrelated lines.
That is what TTP is for.

## 1. Capture a real config

```bash
make config-get DEV=cr1
# saved to automation/configs/cr1.cfg
```

Never write a template against remembered syntax.

## 2. Write the template

`automation/ttp/templates/<platform>-<kind>.txt`, where `platform` is the
Infrahub platform name (`ios_xe`, `vrp`, `routeros`) and `kind` is what you are
extracting (`interfaces`, `acls`, `bgp`).

```
<group name="interfaces*">
interface {{ interface }}
 description {{ description | re(".+") }}
 ip address {{ ip }} {{ mask }}
 shutdown {{ shutdown | set(true) }}
</group>
```

How it works:

- **`<group>`** opens a repeating block. The `*` in `name="interfaces*"` makes
  the result a list rather than a single object.
- **Indentation is matched literally.** The leading space before `description`
  is what scopes it inside the interface stanza — this is the whole point of
  TTP and the easiest thing to get wrong.
- **`{{ name }}`** captures a value.
- **`| re(".+")`** overrides the default word match; use it for anything
  containing spaces, like a description.
- **`| set(true)`** records a flag for a line that has no value of its own.

A line in the template that a config does not contain simply does not match —
it is optional, not required. That is why a template works across devices with
different feature sets.

## 3. Test it

```bash
make config-parsed DEV=cr1
```

Or offline, against a saved config:

```bash
docker compose exec automation python -c "
from automation.ttp import parse
cfg = open('/app/automation/configs/cr1.cfg').read()
import json; print(json.dumps(parse.parse_config('ios_xe', cfg), indent=2))
"
```

## 4. Common failures

| Symptom | Cause |
|---|---|
| `template matched nothing` | indentation does not match the real config — check for tabs vs spaces, and count the leading spaces |
| Only the first stanza is returned | the group name has no `*`, so results collapse to one object |
| A description comes back truncated | the default match stops at whitespace; add `\| re(".+")` |
| Values from one stanza appear under another | the block is not closed — check the indentation of the *next* line after your group |

## 5. Why an empty result is an error

`parse_config` raises `TTPParseError` rather than returning `[]`. A template
that stops matching otherwise looks identical to a device with no
configuration — and the error message says how many lines the config had, so
you can tell a template problem from a genuinely empty response:

```
vrp 'interfaces': template matched nothing. The config has 412 line(s),
so this is a template problem, not an empty device.
```

## 6. Adding a new *kind*

Templates are keyed `<platform>-<kind>.txt`, so adding `ios_xe-acls.txt` makes
this work with no code change:

```bash
curl -s "localhost:${AUTOMATION_PORT}/device/cr1/config/structured?kind=acls"
```

Add the same `kind` for every platform you support, or the endpoint works for
some devices and not others.
