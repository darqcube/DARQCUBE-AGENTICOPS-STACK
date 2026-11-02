# Changelog

Versions are git tags `vMAJOR.MINOR.PATCH` on `main`, newest first here.

- **MAJOR**: something breaks for whoever deploys or calls the stack (an API, an MCP tool, a setting or the site file).
- **MINOR**: new features, backward compatible.
- **PATCH**: fixes only.

A platform that depends on a feature names the version it needs (for example: the approvals path needs 1.0.0 or later).

## 1.1.0 (2026-11-02)

### Features
- **Auto-investigate policy in Infrahub**: three Device attributes (`auto_investigate`, off by default;
  `auto_investigate_interfaces`, `ifName` patterns; `auto_investigate_protocols`) decide which alerts start an AI
  investigation. `make render` turns them into Alertmanager routes to a new `investigate` receiver and reloads
  Alertmanager; every other alert goes to `notify`. Both receivers use the one `ALERT_WEBHOOK_URL`; the body's
  `"receiver"` names the path. BGP is watched today; OSPF, IS-IS, EIGRP and RIP are accepted and reported as not
  watched yet. `docs/how-to/auto-investigate.md`.

### Changes
- Alertmanager reads its config from `observability/telegraf/generated/alertmanager.yml` (written by `make render`;
  config-init copies the template before the first render) and its webhook URL from a file (`url_file`), so the config
  never holds the URL. The receiver formerly named `default` is now `notify`.
- After upgrading: `make schema`, `make render`.

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
