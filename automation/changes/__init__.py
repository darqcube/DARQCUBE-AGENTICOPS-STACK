"""Device changes that a person approves before they reach a device.

WRITE_MODE=approved (the default): a change is STAGED (device, exact lines,
rollback, fingerprint), APPROVED by a person with the approve right through a
path an AI agent cannot use, then COMMITTED once, within a short window, by the
person who asked or the approver. WRITE_MODE=direct keeps the old single-step
push for deployments that approve elsewhere — documented as unsafe.

policy.py   the rules, pure functions: rights, binding, clocks, fingerprint
lines.py    protected lines, refused when a change is staged
store.py    the changes and every decision on them, in SQLite

docs/how-to/approve-changes.md
"""
