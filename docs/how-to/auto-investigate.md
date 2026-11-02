# Choose which alerts start an AI investigation

Every alert reaches the AI platform through one webhook (`ALERT_WEBHOOK_URL`). Alertmanager sends it to one of two
receivers, and the platform reads which from the body's `"receiver"`:

| Receiver | Asks the platform to | Which alerts |
|---|---|---|
| `investigate` | start an AI investigation, read-only, and tell people | those a device's policy in Infrahub names |
| `notify` | tell people | every other alert |

An investigation never changes a device: a change it proposes is still staged, approved by a person and committed
once ([approve-changes.md](approve-changes.md)).

## The policy lives in Infrahub

Three attributes on each **Device**, off unless set:

| Attribute | Value | Empty means |
|---|---|---|
| `auto_investigate` | on / off (default **off**) | – |
| `auto_investigate_interfaces` | comma-separated `ifName` patterns, e.g. `Tu.*, Et0/1` | every interface |
| `auto_investigate_protocols` | comma-separated: `bgp`, `ospf`, `isis`, `eigrp`, `rip` | every protocol the stack watches |

With `auto_investigate` on, these alerts of that device go to `investigate`:

| Alert | When |
|---|---|
| `DeviceUnreachable` | always |
| `InterfaceDown` | its `ifName` matches one of the patterns (or any, when the list is empty) |
| `BGPPeerDown` | `bgp` is listed (or the list is empty) |

A pattern is a regular expression for the **whole** name, as the device reports it in IF-MIB `ifName`. IOS-XE
reports short names (`Gi1`, `Tu2`, `Et0/1`): `Tunnel2` matches nothing there, and `make render` warns about it. A `.`
matches any character, so `Gi0/0.100` also matches `Gi0/0x100`; write `Gi0/0\.100` to be exact.

**Protocols the stack watches today: BGP.** `ospf`, `isis`, `eigrp` and `rip` are accepted and reported by
`make render` ("not watched by the stack yet"): no neighbour state is collected for them, so no alert exists that
could start an investigation.

Set them in the Infrahub UI, or in the device's YAML:

```yaml
# source-of-truth/devices/<yours>.yml
- name: router1
  role: core-wan
  auto_investigate: true
  auto_investigate_interfaces: "Tu.*, Et0/1"
  auto_investigate_protocols: bgp
```

```bash
make schema    # once, after an upgrade that adds the attributes
make seed      # when the YAML changed
make render    # writes observability/telegraf/generated/alertmanager.yml and reloads Alertmanager
```

With [auto-render](auto-render.md) on, a change saved in Infrahub is applied by itself, like any other.

## Check which path an alert takes

`make render` lists the routes it wrote and warns about anything that cannot take effect. To test one alert:

```bash
docker compose exec alertmanager amtool config routes test \
  --config.file=/etc/alertmanager/generated/alertmanager.yml \
  alertname=InterfaceDown device=router1 ifName=Tu2
# investigate
```

## Maintenance

A silence in the Alertmanager UI stops both receivers. Set a device's `status` to `maintenance` to stop polling it.
