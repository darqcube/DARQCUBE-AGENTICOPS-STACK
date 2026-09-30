# Move the stack to a new host

Rebuild the deployment on a different VM — a bigger one, a different
hypervisor, or a customer's machine — keeping its settings and inventory.

Everything that makes a deployment *this* deployment is in three gitignored
files. The repository, the images and the data are rebuilt on the new host.

| Copy | Holds | Without it |
|---|---|---|
| `.env` *or* `site.yml` | credentials, secrets, ports, tuning | the installer prompts again and generates new secrets |
| `source-of-truth/devices/*.yml` | your sites, tags and devices | Infrahub starts empty |

Not copied: metric and log history (Prometheus and Loki volumes) and anything
edited only in the Infrahub UI. Both start fresh — see
[architecture.md — Volumes](../architecture.md#volumes-and-storage) if you need
to carry history across.

## 1. Prepare the new host

Size it first — [prerequisites](../install/01-prerequisites.md#sizing). A VM
below the minimum fails part-way through the install, not at the start.

```bash
git clone <this repo> && cd DARQCUBE-AGENTICOPS-STACK
./scripts/prepare-ubuntu.sh          # packages, Docker, docker group
# log out and back in, then cd back into the repo
```

## 2. Copy the files

From any machine that can reach both hosts. `.env` holds credentials: stream it
rather than leaving a copy on the machine in between, and keep it private.

```bash
OLD=user@<old-host>; NEW=user@<new-host>; REPO='~/DARQCUBE-AGENTICOPS-STACK'

ssh $OLD "cat $REPO/.env" | ssh $NEW "umask 077; cat > $REPO/.env"
ssh $OLD "cd $REPO/source-of-truth/devices && tar cf - *.yml" \
  | ssh $NEW "cd $REPO/source-of-truth/devices && tar xf -"
```

If the old host has a `site.yml`, copy that instead of `.env` — the installer
generates a fresh `.env`, with new secrets, from it.

## 3. Change what belongs to the old host

| Setting | Why |
|---|---|
| `SYSLOG_COLLECTOR_IP` in `.env` (`site.collector_ip` in `site.yml`) | the address devices send syslog and flow to — now the new host |
| device addresses in `devices/*.yml` | only if the new host reaches the devices differently — a different network, or DNS names that do not resolve from it |

```bash
sed -i 's/^SYSLOG_COLLECTOR_IP=.*/SYSLOG_COLLECTOR_IP=<new-host-ip>/' .env
```

## 4. Install

On the new host, **from the repo folder** — `install.py` is looked up relative
to where you are:

```bash
cd ~/DARQCUBE-AGENTICOPS-STACK
sudo python3 install.py --fix-sysctl
python3 install.py
```

It reuses every value already in `.env`, and seeds and renders the copied
inventory.

## 5. Move the devices over

The devices still send syslog and flow to the **old** host. On each device,
point the logging host and flow exporter at the new address — see
[devices/](../devices/) — then check each stage end to end:
[administration/infrahub-guide.md — Verify](../administration/infrahub-guide.md#6-verify-end-to-end).

## 6. Retire the old host

```bash
make down          # on the old host; volumes are kept until you remove them
```

Stopping it avoids devices reporting to two collectors, and frees its ports.
