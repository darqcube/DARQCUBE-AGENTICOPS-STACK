# Deploy on customer premises

For a PoC or small production deployment on someone else's network. The
mechanics are the same as any install — this page is about the decisions you
make *before* running `install.py`, and the ones a customer's security team
will ask about.

## 1. Where the VM goes

**On the management network.** That is the only place it can reach device
management addresses *and* be reached by devices pushing syslog and flow.

One interface on the management VLAN is enough. A second interface for your own
access (or just SSH over the management VLAN) is a customer preference, not a
requirement.

## 2. The firewall matrix

Hand this to whoever runs their firewall. Five rules, and nothing else crosses
the boundary.

| Direction | Protocol / port | Purpose |
|---|---|---|
| VM → device mgmt subnet | UDP 161 | SNMP polling |
| VM → device mgmt subnet | TCP 22 | SSH — config and state |
| device mgmt subnet → VM | UDP 514 (or 1514) | syslog |
| device mgmt subnet → VM | UDP 2055 (or 12055) | NetFlow |
| device mgmt subnet → VM | UDP 4739 (or 14739) | IPFIX |

Two directions, usually two separate rules. Getting one and not the other gives
a device that looks configured and is half working —
[fix-silent-data-loss.md](fix-silent-data-loss.md) covers how that fails.

gNMI (TCP 57400, VM → device) only if you are using it; Cisco IOS-XE only.

## 3. Start with a read-only device account

**The single highest-value decision on this page.**

A PoC almost never needs to change the customer's network. Create the device
account read-only, and the stack physically cannot:

```
! Cisco IOS-XE — privilege 5, or an explicit command allowlist
username darqcube privilege 5 secret <PASSWORD>
```

```
# Huawei VRP — level 1 is monitor-level
local-user darqcube privilege level 1
```

```
# MikroTik RouterOS
/user add name=darqcube group=read password=<PASSWORD>
```

You still get everything that matters: all metrics, all logs, all flow, parsed
operational state, and the assurance checks. What stops working is
`make config-put` and the MCP write tool — which should not be running in a PoC
anyway.

This is also far easier to take to a change board than "a thing with full write
access to your network", and it removes most of the risk in the next section
without any configuration at all.

Move to a privileged account only when the customer explicitly wants
configuration management, and say so in writing when you do.

## 4. Who can reach the UIs

This is the one real decision. By default `install.py` publishes Grafana,
Infrahub, Prometheus, Alertmanager, Loki and the automation API on **every
interface**. Three patterns, all workable:

### a. SSH tunnel — nothing exposed

```bash
ssh -L 3000:localhost:3000 -L 8000:localhost:8000 user@vm
```

Then browse `http://localhost:3000`. Best for a short PoC where only you need
to look. Nothing is reachable from their network at all.

### b. Bind to the management interface

Edit the port lines in `compose/observability.yaml`, `source-of-truth.yaml` and
`automation.yaml` to name the address:

```yaml
    ports:
      - "10.20.0.50:${GRAFANA_PORT:-3000}:3000"
```

**Leave the device-facing UDP ports on all interfaces** — devices must reach
them:

```yaml
      - "${SYSLOG_PORT:-514}:514/udp"        # unchanged
      - "${NETFLOW_PORT:-2055}:2055/udp"     # unchanged
```

This is what most customers expect: visible from the NOC, invisible from the
user LAN.

### c. Open, with a host firewall

```bash
sudo ufw default deny incoming
sudo ufw allow from 10.20.0.0/24 to any port 22 proto tcp
sudo ufw allow from 10.20.0.0/24 to any port 3000 proto tcp   # Grafana
sudo ufw allow from 10.20.0.0/24 to any port 8000 proto tcp   # Infrahub
sudo ufw allow to any port 514 proto udp                      # syslog, from devices
sudo ufw allow to any port 2055 proto udp                     # NetFlow
sudo ufw allow to any port 4739 proto udp                      # IPFIX
sudo ufw enable
```

Some security teams prefer this because it is enforced in one place they
already audit.

## 5. The automation API

Know what it is before you deploy it:

```
GET  /device/{name}/config     dumps a running config
POST /device/{name}/config     pushes configuration to a device
```

**It has no authentication.** Anything that can reach port 8100 can call it.
With a read-only device account (§3) the POST fails at the device, but the GET
still returns running configurations — which contain SNMP communities, local
password hashes and keys.

So on a customer network, do at least one of:

- **Don't publish it.** Comment out the `ports:` block in
  `compose/automation.yaml`. Everything still works — the MCP servers reach it
  over the Docker network, and you can use it from the VM itself with
  `docker compose exec`.
- **Bind it to localhost**: `"127.0.0.1:${AUTOMATION_PORT:-8100}:8100"`.
- **Firewall it** to your own workstation only.

Note that `MCP_ALLOW_WRITE=false` does **not** protect this. That flag gates the
MCP *tool*; the API underneath is always able to push. The full picture, and why
closing the port costs an AI platform nothing:
[connect-an-ai-platform.md](connect-an-ai-platform.md#what-it-does-not-do).

## 6. Credentials

| | |
|---|---|
| `site.yml` | holds the device credentials in plaintext. `chmod 600`, gitignored, and `install.py` refuses to run if it has been committed. |
| `.env` | generated from it, also `chmod 600`. |
| Infrahub | **never** stores device credentials — by design. |
| Running configs | fetched configs land in `automation/configs/` and contain secrets. Gitignored; treat the directory as sensitive. |

Use a **dedicated account** for the stack, not a shared admin login — so the
customer can see exactly what it did in their AAA logs, and revoke it in one
action.

## 7. Ending the engagement

```bash
make clean                    # stops everything and deletes all volumes
rm -f site.yml .env           # the credentials
rm -rf automation/configs/*   # fetched running configs
```

Then have the customer disable the device account you created. Leaving a
working account behind is the thing that gets remembered.

## 8. What to tell their security team

Honest summary, which tends to go down better than a hardening claim:

- It is a **single VM** running containers. Nothing is installed on network devices.
- It needs **SNMP read and SSH** to devices, and **receives syslog and flow**.
- Device credentials are held on that VM in a root-readable file. Nothing is
  stored on the devices, and Infrahub never holds credentials.
- With a **read-only device account** it cannot change anything.
- **There is no TLS between components and no authentication on Prometheus,
  Loki, Alertmanager or the automation API.** It is designed for a management
  network, not for exposure. Access control is the network's job.
- It makes **no outbound connections** unless you configure an alert webhook.

That last point matters more than people expect — there is no telemetry, no
licence check and no phone-home anywhere in the stack.

## Related

- [../INSTALL.md](../INSTALL.md) — the install itself
- [../devices/](../devices/) — device-side configuration per vendor
- [../scale.md](../scale.md) — sizing the VM for their fleet
- [fix-silent-data-loss.md](fix-silent-data-loss.md) — the failures that produce no error
