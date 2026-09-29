# Managing the inventory in Infrahub

How sites, devices and their classification get into Infrahub. The UI is for
learning and small changes; YAML plus `make seed` is for a production fleet of
up to 400 devices. Both write the same schema, and everyday work — a device, a
site, a tag, even a new field — is a data edit, never a code change.

## 1. Before you start

### Your inventory never goes in the repo

This repository is shared. What it ships is the same for everyone: code, the
schema, and made-up examples. What *you* add is per deployment and stays on
your VM:

| Tracked in git — the same for everyone | Yours — gitignored |
|---|---|
| `source-of-truth/schema/darqcube.yml` | `.env`, `site.yml` |
| `source-of-truth/devices/examples/*.yml` | `source-of-truth/devices/*.yml` |

`make seed` reads `source-of-truth/devices/*.yml` and **never** the `examples/`
folder, so a fresh install starts with an empty Infrahub rather than made-up
devices the collectors would then poll. Because your files are gitignored,
`git pull` never conflicts with them and `git add .` cannot publish them.

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
YAML. Use either — but give each device one owner:

| Owner | Good for |
|---|---|
| the UI | learning Infrahub, a lab, a handful of devices |
| the YAML | a production fleet — reviewable, repeatable, and `make seed` rebuilds Infrahub if `neo4j-data` is lost. Keep a backup of the files: they are not in git |

### The device name is a join key

`name` must equal the device's configured hostname exactly. It joins a syslog
line to a metric to an Infrahub record. If they differ, logs arrive labelled
`unknown` and nothing reports it.

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
| `tags` | → Tag, many | free-form, e.g. `pci`, `lab`, a customer name |
| `status` | Dropdown | `active` (default), `provisioning`, `maintenance`, `decommissioned` |
| `telemetry_mode` | Dropdown | `snmp` (default), `gnmi` |
| `snmp_security` | Dropdown | `auth_priv` (default), `auth_no_priv` — only for images that cannot encrypt SNMP, e.g. Cisco L2 IOL |
| `flow_enabled` | Boolean | default `false` |

**Dropdown or tag?** If code or alerting may act on a value, it is a dropdown —
a typo in a dropdown is rejected. Tags are for people filtering and browsing.

**Labels.** `role` is a Prometheus and Loki label, so new role values appear on
metrics and logs automatically. `environment`, `region`, `site_type` and `tags`
are **not** labels: Loki labels stay `device, site, role, severity`.

**Polling by DNS name.** When `management_host` is set, Telegraf polls it and
automation SSHes to it. Use it when the stack reaches devices by name — or
when the name is reachable and the IP is not, as in some container labs. Set
`management_ip` as well if the device exports NetFlow/IPFIX: flow records
arrive from the device's IP address, and the renderer uses that IP to label
them.

### Adding a field

1. Add it to `darqcube.yml` with `optional: true` or a `default_value`, and a
   `description` under 128 characters.
2. `make schema`.
3. Set it in the YAML and `make seed`.

No code changes: `seed.py` reads its field list from the loaded schema. A key
the schema does not know fails the seed with the list of valid fields, so a
typo such as `enviroment:` is never silently dropped. New dropdown values —
another role, another environment — are steps 1 and 2 only.

A field only changes *behaviour* if code reads it. Storing `environment` needs
nothing; "no alerts for `demo` devices" is an alert-rule change.

## 3. Worked example

Two sites, three devices, one tag — in `source-of-truth/devices/sites.yml`:

```yaml
tags:
  - name: lab
    description: Not customer-facing

sites:
  - name: site-a
    site_type: hq
    region: north
  - name: site-b
    site_type: branch
    region: north
```

and `source-of-truth/devices/devices.yml`:

```yaml
devices:
  - name: core-01
    site: site-a
    role: core-wan
    platform: ios_xe
    management_ip: 10.0.0.11
    environment: demo
    tags: [lab]

  - name: edge-01
    site: site-a
    role: internet-edge
    platform: ios_xe
    management_host: edge-01.example.net   # polled and SSHed by name
    environment: demo
    tags: [lab]

  - name: branch-01
    site: site-b
    role: branch-wan
    platform: routeros
    management_ip: 10.0.1.1
    environment: demo
```

## 4. Manual process — the Infrahub UI

UI: `http://<host>:${INFRAHUB_PORT}`, login `admin` / `infrahub` (see
[architecture.md](../architecture.md#web-uis-and-apis)).

1. **Load the schema.** `make schema`. In the UI, open **Schema** and confirm
   `site_type` and `region` on Network Site, and `environment`,
   `management_host` and `tags` on Network Device.
2. **Create an Infrahub branch.** Branch selector (top left) → **+** → e.g.
   `onboard-site-a`. Stay on it for every step below.
3. **Create tags.** Object Management → **Tag**.
4. **Create sites.** Network Site → one per site, with `site_type` set.
5. **Create devices.** Network Device → one form each. Site is a picker; tags
   are a multi-select.
6. **Review.** Open the branch → **Data** diff. Every object you created is
   listed; nothing is on `main` yet.
7. **Propose and merge.** **Proposed Changes** → new, source your branch,
   destination `main` → review → **Merge**.
8. **Render.** `make render`. Telegraf picks it up within 30 s, Logstash within
   60 s.
9. **Verify.** As in [add-a-device.md — step 4](../how-to/add-a-device.md#4-check-it-worked):
   the GraphQL query, a Prometheus series, a Loki line, `make state DEV=<name>`.

To retire a device, set `status` to `decommissioned` rather than deleting it —
polling stops and history is kept.

## 5. YAML process — up to ~400 devices

The same branch and Proposed Change flow as section 4; `make seed` fills the
branch instead of a person. All commands run on the VM, in the repo folder.

1. **Create your files** — once per install:
   `cp source-of-truth/devices/examples/*.yml source-of-truth/devices/`
2. **Write the inventory.** Replace the examples with your own. Every `*.yml`
   in `source-of-truth/devices/` is read, and each may hold `tags:`, `sites:`
   and `devices:`. Past ~50 devices, use one file per region or site —
   `devices-north.yml`, `devices-south.yml` — so a batch is easy to review.
   From a CMDB or IPAM export, generate these files rather than typing them.
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
7. **Change and retire through the same loop** — with a **new** branch name each time: a merged Infrahub branch is read-only, and seed refuses it. Edit YAML →
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
