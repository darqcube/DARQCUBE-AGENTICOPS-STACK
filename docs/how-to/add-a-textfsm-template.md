# Add a TextFSM template

Do this when a parse fails:

```
mikrotik_routeros '/interface print': template (ntc-templates) matched no
lines. The device returned 14 line(s), so this is a template problem, not an
empty device.
```

## 1. Check whether a template already exists

```bash
.venv/bin/python -c "
import ntc_templates, os
d = os.path.join(os.path.dirname(ntc_templates.__file__), 'templates')
rows = [l for l in open(os.path.join(d,'index')).read().splitlines() if ', huawei_vrp,' in l]
print('\n'.join(rows))"
```

The `Command` column uses `[[...]]` for optional abbreviations, so
`dis[[play]] inter[[face]] br[[ief]]` matches both `display interface brief` and
`dis inter br`. Often the template exists but under a slightly different command
— in which case just change `state_cmd` in `platforms.yml`.

## 2. Capture real output

Never write a template against remembered syntax.

```bash
curl -s "localhost:${AUTOMATION_PORT:-8100}/device/mt-01/state?raw=1" \
  | .venv/bin/python -c "import json,sys; print(json.load(sys.stdin)['raw'])" \
  > automation/textfsm/samples/routeros-interface-print-terse.txt
```

Or from inside the container:

```bash
docker compose exec automation \
  python -m automation.netmiko.get_state --device mt-01 --raw
```

## 3. Write the template

`automation/textfsm/templates/<netmiko_type>_<command>.textfsm`:

```
Value Required INTERFACE (\S+)
Value FLAGS (\S*)
Value TYPE (\S+)

Start
  ^\s*\d+\s+${FLAGS}\s+name=${INTERFACE}\s+.*type=${TYPE} -> Record
  ^\s*# -> Next
  ^\s*$$ -> Next
  ^. -> Error
```

Notes that save time:
- `Value Required X` — a row is only recorded if this value matched.
- `-> Record` saves the row; without it nothing is emitted.
- `$$` is a literal `$` (end of line). A single `$` starts a value reference.
- `^. -> Error` is strict. During development use `^. -> Next` so banner lines
  do not abort the parse, then tighten it.

## 4. Register it

`automation/textfsm/index` — first match wins, so put specific before general:

```
Template, Hostname, Platform, Command
mikrotik_routeros_interface_print.textfsm, .*, mikrotik_routeros, /interface print
huawei_vrp_display_interface_brief.textfsm, .*, huawei_vrp, display int[erface] br[ief]
```

`Platform` is the **textfsm_platform** from `platforms.yml`, not the Infrahub
platform name.

## 5. Add it to the manifest

`automation/textfsm/samples/manifest.yml`:

```yaml
  - file: routeros-interface-print-terse.txt
    platform: routeros                       # the INFRAHUB platform name
    command: /interface print terse without-paging
    source: MikroTik RouterOS 7.16           # record the firmware
    expect_keys: [id, name, type]
```

A manifest rather than filename-encoding, because RouterOS commands start with
`/` and commands contain spaces — neither round-trips through a filename.
Recording the firmware version is the first question when a template later
stops matching.

## 6. Test — offline, no devices

```bash
make test-templates
```

## Why every template needs a committed sample

TextFSM returns `[]` for a template that does not match. It does not raise.
`[]` is indistinguishable from "this device has no interfaces", so a broken
template looks exactly like a healthy idle device.

`parse.py` converts that into a `ParseError`, but only a committed sample can
tell you **before** a device does.

## Common failures

| Symptom | Cause |
|---|---|
| `matched no lines` | `^. -> Error` hit a banner line, or no `-> Record` |
| Parses but rows are empty | `Value Required` never matched — check the regex against the sample |
| Wrong column count | vendor pads with variable whitespace; use `\s+`, never a fixed count |
| Works on one device, not another | different firmware — add a second sample and widen the pattern |
