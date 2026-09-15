# Device onboarding

What to configure **on the device** so this stack can monitor and manage it.

- [Cisco IOS-XE](cisco-ios-xe.md)
- [Huawei VRP](huawei-vrp.md)
- [MikroTik RouterOS](mikrotik-routeros.md)

## The contract, whatever the vendor

Four feeds, and a device needs all four to be fully visible:

| Feed | Direction | Port | Gives the stack |
|---|---|---|---|
| **SNMPv3** | stack polls the device | UDP 161 | CPU, memory, interfaces |
| **Syslog** | device pushes to the stack | UDP `${SYSLOG_PORT}` | events |
| **NetFlow / IPFIX** | device pushes to the stack | UDP `${NETFLOW_PORT}` | traffic volume |
| **SSH** | stack connects to the device | TCP 22 | config and state, get and put |

Plus the fifth, which is not on the device at all: a record in Infrahub saying
the device should exist.

## Reachability — both directions, and they are different

Two separate requirements, often two separate firewall rules. Getting one and
not the other gives you a half-working device that looks fine in Infrahub.

### The stack reaches out to the device

| From | To | Port | For |
|---|---|---|---|
| Telegraf | device | UDP 161 | SNMP polling |
| Telegraf | device | TCP 57400 | gNMI (Cisco only, optional) |
| Automation | device | TCP 22 | SSH — config and state, get and put |

The automation container is the only thing that opens an SSH session, and it
takes its target list **from Infrahub**: the source of truth is what tells the
stack which devices exist and at which address. Add a device there and it
becomes reachable to every tool; set its `status` to anything but `active` and
the stack stops touching it.

### The device reaches back to the stack

| From | To | Port | For |
|---|---|---|---|
| device | Logstash | UDP `${SYSLOG_PORT}` | syslog |
| device | Telegraf | UDP `${NETFLOW_PORT}` | NetFlow |
| device | Telegraf | UDP `${IPFIX_PORT}` | IPFIX |

These are push feeds, so the device needs a route to **`SYSLOG_COLLECTOR_IP`** —
the stack host's own routable address. Nothing tells you if this is missing:
the device sends, nothing arrives, and no error appears at either end.

Check both directions before blaming the configuration:

```bash
# stack -> device
docker compose exec telegraf nc -zvu <device-ip> 161
docker compose exec automation nc -zv <device-ip> 22

# device -> stack (run on the stack host while the device is sending)
sudo tcpdump -ni any udp port ${SYSLOG_PORT} -c 5
```

## Two things that catch people out

**The device's hostname must exactly equal its `name` in `devices.yml`.** It is
the key joining a syslog line to a metric to an Infrahub record. Mismatch and
logs arrive labelled `unknown`, metrics and logs stop correlating, and nothing
reports the problem.

**Devices send to the VM's routable IP**, which is `SYSLOG_COLLECTOR_IP` in
`.env`. Not a container IP, not `127.0.0.1`. The stack polls *out* to the device
and the device pushes *in* to the VM — two directions, often two different
firewall rules.

## Ports

The ports devices send to are configurable. Defaults ship non-standard:

| Feed | Default | Standard |
|---|---|---|
| syslog | 1514 | 514 |
| NetFlow | 12055 | 2055 |
| IPFIX | 14739 | 4739 |

Port 514 needs root on most Linux hosts, which is why 1514 is the default.
Change them in `.env` **and** on every device —
[../how-to/change-ports.md](../how-to/change-ports.md).

## NTP

Configure it on every device. Metrics and logs are correlated by timestamp, and
a device with a drifting clock produces a timeline that looks wrong in ways that
are hard to attribute.
