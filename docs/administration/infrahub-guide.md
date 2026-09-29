# Managing the inventory in Infrahub

How sites, devices and their classification get into Infrahub. The UI is for
learning and small changes; the YAML-plus-seed pipeline is for a production
fleet of up to 400 devices. Both use the same schema.

> **Status.** The schema additions in [section 2](#2-schema-additions) are the
> target design. They are **not yet in** `source-of-truth/schema/darqcube.yml`.
> Until they are, the manual process works with the fields that exist today
> (`name`, `role`, `platform`, `management_ip`, `status`, `site`).

## 1. Before you start

### "Branch" means two things

| Word | Meaning here | Modelled as |
|---|---|---|
| **branch office** | a remote site | a Site with `site_type: branch` |
| **Infrahub branch** | an isolated copy of the data, like a Git branch | Infrahub's own branching |

Every change in this guide happens on an **Infrahub branch** and reaches
`main` through a Proposed Change. `make render` and the automation service read
`main` (`INFRAHUB_BRANCH`, default `main`), so a device on an unmerged branch is
never polled or connected to. That is the review gate.

### The UI and the YAML write the same records

`make seed` upserts from `source-of-truth/devices/*.yml`. If you change a
device in the UI and it is also in the YAML, the next `make seed` puts the YAML
value back. Choose one owner per environment:

| Environment | Owner | Why |
|---|---|---|
| lab / demo | the UI | learning Infrahub is the point |
| production | the YAML | reviewable, and `make schema && make seed` rebuilds Infrahub from the repo if `neo4j-data` is lost |

### The device name is a join key

`name` must equal the device's configured hostname exactly. It joins a syslog
line to a metric to an Infrahub record. For containerlab, the node name
(`cr1`, `b-north`) is the hostname.

## 2. Schema additions

All additions are optional or have a default, so existing devices stay valid
the moment the schema loads. Keep every `description:` under 128 characters —
see the header of `darqcube.yml`.

### Site

| Attribute | Kind | Values | Purpose |
|---|---|---|---|
| `site_type` | Dropdown | `hq`, `branch`, `datacenter`, `pop`, `lab` — default `branch` | "branch office" is a property of a site, not a second model |
| `region` | Text, optional | free text, e.g. `north` | groups a large fleet into onboarding batches and views |

### Device

| Attribute | Kind | Values | Purpose |
|---|---|---|---|
| `role` | Dropdown (extend) | add `core-wan`, `core-dc`, `internet-edge`, `branch-wan` to the existing list | what the device does |
| `environment` | Dropdown | `production`, `staging`, `lab`, `demo` — default `production` | separates demo from production |
| `tags` | Relationship → built-in `BuiltinTag`, many, optional | free text | ad-hoc grouping: `containerlab`, `nbp-wan`, `pci`, a customer name |
| `management_host` | Text, optional | an FQDN | see below |

**Dropdown or tag?** If code or alerting may act on a value, it is a dropdown —
a typo in a tag (`prodution`) is silently ignored, a typo in a dropdown is
rejected. Tags are for people filtering and browsing.

**Labels.** `role` is already a Prometheus and Loki label, so new role values
appear on metrics and logs automatically. Nothing in alert rules or dashboards
matches on a specific role value today. `environment`, `region` and `tags` do
**not** become labels: Loki labels stay `device, site, role, severity`.

**`management_host`.** `management_ip` is an `IPHost`, so it cannot hold an
FQDN such as `clab-nbp-wan-clab-cr1.orb.local`. The renderer and the Nornir
inventory both connect to `management_ip`. Using FQDNs needs this field
**and** a change to `render-inventory.py` and `automation/nornir/tasks.py` to
prefer it when set. Until then, use the IP.

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
`nbp-wan`.

List the running lab and each node's OrbStack name:

```bash
docker ps --filter label=containerlab --format '{{.Names}}'
# FQDN = <container-name>.orb.local, e.g. clab-nbp-wan-clab-cr1.orb.local
```

## 4. Manual process — the Infrahub UI

UI: `http://<vm-ip>:${INFRAHUB_PORT}`, login `admin` / `infrahub` (see
[architecture.md](../architecture.md#web-uis-and-apis)).

1. **Load the schema.** `make schema`. In the UI, open **Schema** and confirm
   the new attributes are listed on Network Site and Network Device.
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

To retire a device, set `status: decommissioned` rather than deleting it —
polling stops and history is kept.

## 5. Automated process — ~400 devices

The same Infrahub branch and Proposed Change flow as section 4; a script fills
the branch instead of a person.

1. **Start from an export.** A CSV or spreadsheet from the CMDB or IPAM with
   `name, site, role, platform, address, environment, tags`. For containerlab,
   `containerlab inspect --format json` is the export.
2. **Generate the YAML.** Convert the export to `sites.yml` and device files.
   Past ~50 devices, split into one file per region or site so reviews stay
   readable. *(Needs `seed.py` to read `devices/*.yml`; today it reads one
   `devices.yml`.)*
3. **Validate before writing.** `seed.py` already rejects an unknown site or
   platform before touching Infrahub. At 400 devices also reject duplicate
   names and duplicate addresses — the most common bulk mistake.
4. **Seed to an Infrahub branch, not `main`.**
   `INFRAHUB_BRANCH=onboard-batch-01 make seed`
5. **Review and merge** the branch as a Proposed Change (section 4, steps 6–7).
6. **Render once**, after the merge — not per device.
7. **Onboard in batches of ~50.** Confirm each batch appears in Prometheus and
   Loki before the next. A wrong community string on 50 devices is a short
   investigation; on 400 it is not.
8. **Change and retire through the same loop:** edit YAML → seed to a branch →
   merge → render.

`seed.py` makes one query and one save per object, sequentially. 400 devices
take a few minutes, which is acceptable for a batch job.

**Alternatives Infrahub offers natively** — `infrahubctl object load` for YAML
object files, and Git repository sync. They work, but `seed.py` stays the
primary path: its validation, the renderer and this stack's tests are built
around it.

## Related

- [add-a-device.md](../how-to/add-a-device.md) — the single-device procedure
- [devices/](../devices/) — per-platform device configuration
- [scale.md](../scale.md) — the numbers behind the 400-device ceiling
