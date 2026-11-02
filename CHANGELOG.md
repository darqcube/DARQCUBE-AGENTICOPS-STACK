# Changelog

Versions are git tags `vMAJOR.MINOR.PATCH` on `main`, newest first here.

- **MAJOR**: something breaks for whoever deploys or calls the stack (an API, an MCP tool, a setting or the site file).
- **MINOR**: new features, backward compatible.
- **PATCH**: fixes only.

A platform that depends on a feature names the version it needs (for example: the approvals path needs 1.0.0 or later).

## 1.4.2 (2026-11-03)

### Fixes
- `OSPFNeighborDown` no longer fires when the router stops answering SNMP: an adjacency counts as gone only while the
  router still reports its interfaces. A 4-minute polling gap raised it for every adjacency at once; a silent router is
  `DeviceNotReporting`'s. The "OSPF sessions" table on Network Overview follows the same rule.

## 1.4.1 (2026-11-02)

### Fixes
- `make render` recreates infrahub-server first when `platforms.yml` changed since it started: the file is a single-file
  mount, so after `git checkout` of a release the container still read the old one, and the render silently left out
  what the release added (1.4.0's OSPF poll on the first VM that upgraded).

## 1.4.0 (2026-11-02)

### Features
- **OSPF adjacencies**: platforms with `ospf: true` (IOS-XE, verified) poll OSPF-MIB's neighbour table
  (`ospf_neighbor_state`, tags `neighbor`, `router_id`). `ospf:neighbor:down` counts an adjacency neither full nor
  two-way, or gone from the router's table in the last hour; **`OSPFNeighborDown`** fires after 2 minutes.
- **Network Overview**: "OSPF sessions down" and an "OSPF sessions" table (every adjacency, its state, worst first).
- **Auto-investigate**: `ospf` in a device's `auto_investigate_protocols` now routes `OSPFNeighborDown` to
  `investigate`.

### Changes
- A decided change's `ChangeWaitingForApproval` is reported resolved within about a minute (`group_interval: 1m` on its
  route), not at the 5-minute group interval.
- After upgrading: `make render` (the OSPF poll and the Alertmanager config), and reload Prometheus's rules.

## 1.3.0 (2026-11-02)

### Changes
- **`ChangeWaitingForApproval` is one alert per waiting change** (from `change_state`), labelled `change_id`, and
  Alertmanager groups it per change: a change that starts waiting while another waits is sent on its own within
  `group_wait`, instead of waiting for the first one's group. Its summary names the change. Before, one alert counted all
  waiting changes (`sum(changes{state="waiting"})`).

## 1.2.0 (2026-11-02)

### Features
- **`change_state{change_id, state}`** on the automation API's `/metrics`: each change staged in the last 24 hours with
  its current state (only the id and the state). A platform shows the person who asked for a change that it was approved
  without a sign-in to the approvals path. `docs/how-to/approve-changes.md`, "Following one change".
- **`--fresh-signin-claim auth_time|iat`** on `scripts/ai-platform-connect.py`: the overlay carries the sign-in claim, so
  re-applying it no longer puts `auth_time` back on a platform that needs `iat` (every approval was refused as not fresh).

## 1.1.1 (2026-11-02)

### Fixes
- Alertmanager did not start on 1.1.0: its generated config was mounted inside its read-only `/etc/alertmanager`
  volume. It is now mounted at `/etc/alertmanager-generated`; a test refuses any volume mounted inside another.

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
