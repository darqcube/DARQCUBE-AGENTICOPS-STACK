# Managing the inventory in Infrahub

How sites, devices and their classification get into Infrahub. The UI is for
learning and small changes; YAML plus `make seed` is for a production fleet of
up to 400 devices. Both write the same schema, and everyday work — a device, a
site, a tag, even a new field — is a YAML edit, never a code change.

## 1. Before you start

### "Branch" means two things

| Word | Meaning here | Modelled as |
|---|---|---|
| **branch office** | a remote site | a Site with `site_type: branch` |
| **Infrahub branch** | an isolated copy of the data, like a Git branch | Infrahub's own branching |

Every change in this guide happens on an **Infrahub branch** and reaches
`main` through a Proposed Change. `make render` and the automation service read
`main`, so a device on an unmerged branch is never polled or connected to.
That is the review gate.

### The UI and the YAML write the same records

`make seed` treats the YAML as the **whole record**: a field left out is reset
to its default, and a device's tags become exactly the list given. A value you
changed in the UI is overwritten by the next seed if that device is in the
YAML. Choose one owner per environment:

| Environment | Owner | Why |
|---|---|---|
| lab / demo | the UI | learning Infrahub is the point |
| production | the YAML | reviewable, and `make schema && make seed` rebuilds Infrahub from the repo if `neo4j-data` is lost |

The shipped `source-of-truth/devices/devices.yml` holds three **example**
devices, one of them named `cr1`. If you create the lab's `cr1` in the UI and
then run `make seed`, the example overwrites it. Replace or delete the
examples before seeding.

### The device name is a join key

`name` must equal the device's configured hostname exactly. It joins a syslog
line to a metric to an Infrahub record. For containerlab, the node name
(`cr1`, `b-north`) is the hostname.

## 2. The schema

Defined in `source-of-truth/schema/darqcube.yml`, loaded with `make schema`.

### Site

| Attribute | Kind | Values |
|---|---|---|
| `name` | Text, unique | short identifier — the `site` metric and log label |
| `site_type` | Dropdown | `hq`, `branch`, `datacenter`, `pop`, `lab` — default `branch` |
| `region` | Text, optional | free text, e.g. `north` |
| `description` | Text, optional | |

### Device

| Attribute | Kind | Values |
|---|---|---|
| `name` | Text, unique | the device's hostname |
| `site` | → Site, required | |
| `role` | Dropdown | `core`, `distribution`, `access`, `wan`, `edge`, `firewall`, `core-wan`, `core-dc`, `internet-edge`, `branch-wan` |
| `platform` | Dropdown | `ios_xe`, `vrp`, `routeros` — must match `platforms.yml` |
| `management_ip` | IPHost | one of `management_ip` / `management_host` is required |
| `management_host` | Text | DNS name; **used instead of** `management_ip` when set |
| `environment` | Dropdown | `production`, `staging`, `lab`, `demo` — default `production` |
| `tags` | → Tag, many | free-form, e.g. `containerlab`, `pci`, a customer name |
| `status` | Dropdown | `active` (default), `provisioning`, `maintenance`, `decommissioned` |
| `telemetry_mode` | Dropdown | `snmp` (default), `gnmi` |
| `flow_enabled` | Boolean | default `false` |

**Dropdown or tag?** If code or alerting may act on a value, it is a dropdown —
a typo in a dropdown is rejected. Tags are for people filtering and browsing.

**Labels.** `role` is a Prometheus and Loki label, so new role values appear on
metrics and logs automatically. `environment`, `region`, `site_type` and `tags`
are **not** labels: Loki labels stay `device, site, role, severity`.

**Polling by DNS name.** When `management_host` is set, Telegraf polls it and
automation SSHes to it. Set `management_ip` as well if the device exports
NetFlow/IPFIX: flow records arrive from the device's IP address, and the
renderer uses that IP to label them.

### Adding a field

1. Add it to `darqcube.yml` with `optional: true` or a `default_value`, and a
   `description` under 128 characters.
2. `make schema`.
3. Set it in the YAML and `make seed`.

No code changes: `seed.py` reads its field list from the loaded schema. A key
the schema does not know fails the seed with the list of valid fields, so a
typo such as `enviroment:` is never silently dropped. New dropdown values —
another role, another environment — are step 1 and 2 only.

A field only changes *behaviour* if code reads it. Storing `environment` needs
nothing; "no alerts for `demo` devices" is an alert-rule change.

## 3. Reference mapping — the `nbp-wan` containerlab

| Site | `site_type` | Devices | Role |
|---|---|---|---|
| `nbp-hq` | `hq` | cr1, cr2, cr3, dr1 | `core-wan` |
| | | cs1 | `core-dc` |
| | | ir1 | `internet-edge` |
| `nbp-branch-north` | `branch` | b-north | `branch-wan` |
| `nbp-branch-central` | `branch` | b-central | `branch-wan` |
| `nbp-branch-south` | `branch` | b-south | `branch-wan` |
| `telco` | `pop` | telco1, telco2 | `wan` — simulated provider network |

All eleven: `platform: ios_xe`, `environment: demo`, tags `containerlab` and
`nbp-wan`, `management_host: clab-nbp-wan-clab-<node>.orb.local`.

List the running lab and each node's OrbStack name:

```bash
docker ps --filter label=containerlab --format '{{.Names}}'
# FQDN = <container-name>.orb.local, e.g. clab-nbp-wan-clab-cr1.orb.local
```

## 4. Manual process — the Infrahub UI

UI: `http://<host>:${INFRAHUB_PORT}`, login `admin` / `infrahub` (see
[architecture.md](../architecture.md#web-uis-and-apis)).

1. **Load the schema.** `make schema`. In the UI, open **Schema** and confirm
   `site_type` and `region` on Network Site, and `environment`,
   `management_host` and `tags` on Network Device.
2. **Create an Infrahub branch.** Branch selector (top left) → **+** → name it
   `onboard-nbp-wan`. Stay on it for every step below.
3. **Create tags.** Object Management → **Tag** → `containerlab`, `nbp-wan`.
4. **Create sites.** Network Site → one per row of the mapping, with
   `site_type` set.
5. **Create devices.** Network Device → one form each. Site is a picker; tags
   are a multi-select.
6. **Review.** Open the branch → **Data** diff. Every object you created is
   listed; nothing is on `main` yet.
7. **Propose and merge.** **Proposed Changes** → new, source
   `onboard-nbp-wan`, destination `main` → review → **Merge**.
8. **Render.** `make render`. Telegraf picks it up within 30 s, Logstash within
   60 s.
9. **Verify.** As in [add-a-device.md — step 4](../how-to/add-a-device.md#4-check-it-worked):
   the GraphQL query, a Prometheus series, a Loki line, `make state DEV=cr1`.

To retire a device, set `status` to `decommissioned` rather than deleting it —
polling stops and history is kept.

## 5. Automated process — ~400 devices

The same branch and Proposed Change flow as section 4; `make seed` fills the
branch instead of a person.

1. **Start from an export.** A CSV or spreadsheet from the CMDB or IPAM with
   `name, site, role, platform, address, environment, tags`. For containerlab,
   `containerlab inspect --format json` is the export.
2. **Write the YAML.** Every `*.yml` in `source-of-truth/devices/` is read, and
   each may hold `tags:`, `sites:` and `devices:`. Past ~50 devices, use one
   file per region or site — `devices-north.yml`, `devices-south.yml` — so a
   review shows one batch.
3. **Seed onto a branch.** `make seed BRANCH=onboard-batch-01`. The Infrahub
   branch is created if it does not exist. Before anything is written, every
   record in every file is checked:
   - every field exists in the schema, and every dropdown value is allowed
   - every site and tag a device names exists (in the YAML or in Infrahub)
   - every platform is in `platforms.yml`
   - every device has `management_ip` or `management_host`
   - no name is defined twice across files

   One failure writes nothing, and every failure is listed at once.
4. **Review and merge** the branch as a Proposed Change (section 4, steps 6–7).
5. **Render once**, after the merge — not per device.
6. **Onboard in batches of ~50.** Confirm each batch appears in Prometheus and
   Loki before the next. A wrong community string on 50 devices is a short
   investigation; on 400 it is not.
7. **Change and retire through the same loop:** edit YAML →
   `make seed BRANCH=…` → merge → `make render`.

`make seed` with no `BRANCH` writes to `main` directly — fine for a lab, not
for production.

**Alternatives Infrahub offers natively** — `infrahubctl object load` for YAML
object files, and Git repository sync. They work, but `make seed` stays the
primary path: it checks platforms against `platforms.yml`, and the renderer
and this stack's tests are built around it.

## Related

- [add-a-device.md](../how-to/add-a-device.md) — the single-device procedure
- [devices/](../devices/) — per-platform device configuration
- [scale.md](../scale.md) — the numbers behind the 400-device ceiling
