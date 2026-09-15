# Captured CLI output

Real command output from each supported platform, used as fixtures by
`automation/tests/test_templates.py` (`make test-templates` — offline, no
devices, runs in seconds).

## Naming

    <platform>__<command with spaces as underscores>.txt

`platform` is the Infrahub platform value (`ios_xe`, `vrp`, `routeros`), not a
netmiko device_type. The test derives the command back from the filename, so
the name is the contract.

## Why every template needs a sample

A TextFSM template is a regex against one firmware's exact output format. When
it stops matching it does not raise — TextFSM returns `[]`, which is
indistinguishable from "this device has no interfaces". `parse.py` turns that
into a `ParseError`, but only a committed sample can tell you *before* a
device does.

Capture one with:

    make state DEV=<name>                     # parsed
    curl -s localhost:${AUTOMATION_PORT}/device/<name>/state?raw=1 \
      > automation/textfsm/samples/<platform>__<command>.txt
