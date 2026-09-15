# Huawei VRP

Syntax below targets **VRP8**. It differs between VRP5 and VRP8 and across
product lines (S-series, CE, NE, AR) — **apply to one switch and confirm before
fanning out**, especially `stelnet server enable` vs `ssh server enable` and any
management VPN-instance binding.

## 1. sysname and SSH

```
sysname sw-hw-01                          # MUST equal the name in devices.yml
#
aaa
 local-user darqcube password irreversible-cipher <PASSWORD>
 local-user darqcube privilege level 15
 local-user darqcube service-type ssh
#
rsa local-key-pair create                 # once; generates the SSH host key
stelnet server enable                     # some trains: ssh server enable
ssh user darqcube
ssh user darqcube authentication-type password
ssh user darqcube service-type stelnet
#
user-interface vty 0 4
 authentication-mode aaa
 protocol inbound ssh
 user privilege level 15
```

`sysname` is load-bearing — VRP puts it in the syslog header, which is how the
identity join lands.

## 2. SNMPv3

```
snmp-agent
snmp-agent sys-info version v3
#
snmp-agent mib-view DARQCUBE include iso
snmp-agent group v3 DARQCUBE privacy read-view DARQCUBE
snmp-agent usm-user v3 darqcube DARQCUBE authentication-mode sha2-256 <AUTH_PASS> privacy-mode aes128 <PRIV_PASS>
```

> VRP uses **sha2-256 / aes128**, where Cisco uses sha / aes 128. The stack's
> Telegraf profile is configured for SHA/AES; if your VRP train insists on
> sha2-256 only, that is a per-platform difference to reflect in the profile
> template.

If management lives in a VPN-instance, bind the agent to it:

```
snmp-agent protocol source-interface <mgmt-interface>
```

### What is collected

| Metric | OID | MIB |
|---|---|---|
| `cpu_usage` | `1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7` | HUAWEI-ENTITY-EXTENT-MIB |
| `memory_used` | `1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11` | HUAWEI-ENTITY-EXTENT-MIB |
| `device_uptime` | `1.3.6.1.2.1.1.3.0` | SNMPv2-MIB |
| interface status, counters, errors, speed | IF-MIB | standard |

> **Huawei reports memory as a percentage already**, unlike Cisco (bytes) and
> MikroTik (allocation units). `platforms.yml` records this as
> `memory.kind: percent` and the Prometheus recording rule uses it. Getting it
> wrong makes a dashboard read 847,000,000%.
>
> Both tables are indexed per physical entity, so each arrives as its own series
> tagged with the index. Use `device:cpu_usage:max` for one value per device.

## 3. Syslog

```
info-center enable
info-center timestamp log date precision-time millisecond
info-center loghost source <mgmt-interface>
info-center loghost <SYSLOG_COLLECTOR_IP> facility local7
```

Add `vpn-instance <mgmt-vpn>` to the loghost line if management is in a VPN
instance.

The VRP format is
`<190>Sep 14 2026 10:23:45+05:00 sw-hw-01 %%01IFNET/4/LINK_STATE(l)[0]:<text>` —
sysname after the timestamp, and module/severity/mnemonic packed into a `%%NN`
prefix rather than Cisco's `%FACILITY-SEV-MNEMONIC`. The Logstash pattern
handles it.

## 4. NetFlow

```
ip netstream export version 9
ip netstream export source <mgmt-ip>
ip netstream export host <SYSLOG_COLLECTOR_IP> 12055
ip netstream timeout active 1
#
interface GigabitEthernet0/0/1
 ip netstream inbound
 ip netstream outbound
```

Set `flow_enabled: true` in `devices.yml`.

## 5. NTP

```
ntp-service unicast-server <NTP_IP>
clock timezone UTC add 00:00:00
```

## 6. One-shot block

```
system-view
 sysname sw-hw-01
 aaa
  local-user darqcube password irreversible-cipher <PASSWORD>
  local-user darqcube privilege level 15
  local-user darqcube service-type ssh
 quit
 rsa local-key-pair create
 stelnet server enable
 ssh user darqcube
 ssh user darqcube authentication-type password
 ssh user darqcube service-type stelnet
 user-interface vty 0 4
  authentication-mode aaa
  protocol inbound ssh
  user privilege level 15
 quit
 snmp-agent
 snmp-agent sys-info version v3
 snmp-agent mib-view DARQCUBE include iso
 snmp-agent group v3 DARQCUBE privacy read-view DARQCUBE
 snmp-agent usm-user v3 darqcube DARQCUBE authentication-mode sha2-256 <AUTH_PASS> privacy-mode aes128 <PRIV_PASS>
 info-center enable
 info-center timestamp log date precision-time millisecond
 info-center loghost source <mgmt-interface>
 info-center loghost <SYSLOG_COLLECTOR_IP> facility local7
 ntp-service unicast-server <NTP_IP>
 clock timezone UTC add 00:00:00
quit
save
```

## 7. Add it to the stack

```yaml
  - name: sw-hw-01
    site: hq
    role: access
    platform: vrp
    management_ip: 10.0.0.21
    flow_enabled: false
```

```bash
make seed && make render
```

## 8. Verify

```bash
source .env
docker compose exec telegraf nc -zvu 10.0.0.21 161
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=cpu_usage{device=\"sw-hw-01\"}"
curl -sG "localhost:${LOKI_PORT}/loki/api/v1/query_range" --data-urlencode 'query={device="sw-hw-01"}'
make state DEV=sw-hw-01
```

## Assurance works the same as every other platform

Operational state comes from **Netmiko + TextFSM**, and the assurance rules that
run against Cisco run identically here — `automation/assurance/normalise.py`
maps VRP's `phy` / `protocol` columns to the same shape.

```bash
make check DEV=sw-hw-01     # the same rules that run against Cisco
make snapshot DEV=sw-hw-01  # comparable state, for pre/post comparison
```

`ntc-templates` ships 39 `huawei_vrp` templates, so most `display` commands
parse without writing anything.

> VRP marks an administratively-down port with a leading `*` in the `phy`
> column, which is how the normaliser tells "shut on purpose" from "down and
> shouldn't be".

## gNMI

VRP supports it on some versions and hardware, but this stack does not enable it
for VRP — SNMP is the metrics path. It is scoped out rather than half-supported.
