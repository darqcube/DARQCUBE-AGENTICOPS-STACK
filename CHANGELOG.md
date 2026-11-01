# Changelog

Versions are git tags `vMAJOR.MINOR.PATCH` on `main`, newest first here.

- **MAJOR**: something breaks for whoever deploys or calls the stack (an API, an MCP tool, a setting or the site file).
- **MINOR**: new features, backward compatible.
- **PATCH**: fixes only.

A platform that depends on a feature names the version it needs (for example: the approvals path needs 1.0.0 or later).

## 1.0.0 (2026-11-01)

The first versioned release: what a single-VM deployment offers an AI platform, with every device change approved by
a person.

### Features
- **Approved changes** (`WRITE_MODE=approved`, the default with writes on): an agent stages a change; a person with the
  approve right approves exactly that change on the approvals path (its own token audience, a sign-in under 5 minutes
  old, four eyes optional); it is committed once, within its window, with its fingerprint unchanged. Rollback once,
  within an hour. Rights (propose, approve, commit, commit any) are roles in the token. Protected lines are refused at
  staging. Every action is recorded with the person and the calling client. `docs/how-to/approve-changes.md`.
- **Learning that a change waits**: `changes{state}` on the automation API's `/metrics`, and the
  `ChangeWaitingForApproval` alert.
- **`CHANGE_FRESH_SIGNIN_CLAIM`** (`auth_time` or `iat`), for identity providers that record no `auth_time` on the
  password grant.
- `WRITE_MODE=direct` keeps the one-step `push_device_config` for platforms that need it.

### Security
- The automation API is published on loopback by default (`AUTOMATION_BIND_IP`): it has no authentication of its own,
  and its one-step push is refused in approved mode.

### Included from before 1.0.0
Source of truth (Infrahub), observability (Telegraf, Logstash, Prometheus, Loki, Alertmanager, Grafana), the automation
API with assurance rules, and seven MCP servers (inventory, metrics, logs, dashboards, device state and changes,
assurance, pyATS) for Cisco IOS-XE, Huawei VRP and MikroTik RouterOS. Earlier history is in `git log`.
