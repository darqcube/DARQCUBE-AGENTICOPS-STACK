# Cisco IOS-XE

## 1. Hostname and SSH

```
hostname cr1                             ! MUST equal the name in devices.yml
!
ip domain name lab
username darqcube privilege 15 secret <PASSWORD>
!
crypto key generate rsa modulus 2048     ! once, enables SSH
ip ssh version 2
!
line vty 0 4
 login local
 transport input ssh
```

Set `DEVICE_USER=darqcube` and `DEVICE_PASSWORD` in `.env`.

## 2. SNMPv3

```
snmp-server view DARQCUBE iso included
snmp-server group DARQCUBE v3 priv read DARQCUBE
snmp-server user darqcube DARQCUBE v3 auth sha <AUTH_PASS> priv aes 128 <PRIV_PASS>
```

Must match `SNMPV3_USER`, `SNMPV3_AUTH` and `SNMPV3_PRIV` in `.env`.

### Devices with no SNMP privacy

Some images — notably the L2 Cisco IOL build — have no privacy support at all:
`priv aes 128` is rejected outright. Use authNoPriv:

```
snmp-server group DARQCUBE v3 auth read DARQCUBE
snmp-server user darqcube DARQCUBE v3 auth sha <AUTH_PASS>
```

The Telegraf profile in this stack is authPriv. Supporting a mixed fleet means
sharding the SNMP config by security level — do not solve it by lowering the
whole network.

### What is collected

| Metric | OID | MIB |
|---|---|---|
| `cpu_usage` | `1.3.6.1.4.1.9.9.109.1.1.1.1.8` | CISCO-PROCESS-MIB |
| `memory_used` / `memory_free` | `…9.9.221.1.1.1.1.18` / `.20` | CISCO-ENHANCED-MEMPOOL-MIB |
| `device_uptime` | `1.3.6.1.2.1.1.3.0` | SNMPv2-MIB |
| interface status, counters, errors, speed | IF-MIB | standard |

> Cisco IOL has no real dataplane, so CPU and memory may return nothing. That is
> the emulator, not the stack — IF-MIB works fully. On real hardware all rows
> populate.

## 3. Syslog

```
service timestamps log datetime msec show-timezone
logging origin-id hostname               ! makes the syslog hostname == the device name
logging host <SYSLOG_COLLECTOR_IP> transport udp port 1514
logging trap informational
```

> **IOS does not emit conformant RFC3164.** The real wire format is
> `<189>264: cr1: *Jul 27 18:45:22.658 UTC: %SSH-5-SSH2_SESSION: <text>` — a
> sequence counter and the origin-id hostname arrive *before* the timestamp, and
> a leading `*` means the clock is not yet NTP-synchronised. A strict RFC3164
> parser rejects every line and drops it silently. The Logstash pattern in this
> stack handles it; nothing on the device needs changing.

## 4. NetFlow

```
flow record DARQCUBE-RECORD
 match ipv4 source address
 match ipv4 destination address
 match ipv4 protocol
 match transport source-port
 match transport destination-port
 collect counter bytes
 collect counter packets
!
flow exporter DARQCUBE-EXPORTER
 destination <SYSLOG_COLLECTOR_IP>
 transport udp 12055
 export-protocol netflow-v9
!
flow monitor DARQCUBE-MONITOR
 exporter DARQCUBE-EXPORTER
 record DARQCUBE-RECORD
 cache timeout active 60
!
interface GigabitEthernet0/0/1
 ip flow monitor DARQCUBE-MONITOR input
 ip flow monitor DARQCUBE-MONITOR output
```

Apply the monitor to every interface you want visibility on. Set
`flow_enabled: true` in `devices.yml`.

## 5. NTP

```
clock timezone UTC 0 0
ntp server <NTP_IP>
```

## 6. One-shot block

```
conf t
 hostname cr1
 ip domain name lab
 username darqcube privilege 15 secret <PASSWORD>
 ip ssh version 2
 line vty 0 4
  login local
  transport input ssh
 exit
 snmp-server view DARQCUBE iso included
 snmp-server group DARQCUBE v3 priv read DARQCUBE
 snmp-server user darqcube DARQCUBE v3 auth sha <AUTH_PASS> priv aes 128 <PRIV_PASS>
 service timestamps log datetime msec show-timezone
 logging origin-id hostname
 logging host <SYSLOG_COLLECTOR_IP> transport udp port 1514
 logging trap informational
 clock timezone UTC 0 0
 ntp server <NTP_IP>
end
write memory
```

## 7. Add it to the stack

```yaml
# source-of-truth/devices/devices.yml
  - name: cr1
    site: hq
    role: core
    platform: ios_xe
    management_ip: 10.0.0.11
    flow_enabled: true
```

```bash
make seed && make render
```

## 8. Verify

```bash
source .env
docker compose exec telegraf nc -zvu 10.0.0.11 161
curl -s "localhost:${PROMETHEUS_PORT}/api/v1/query?query=device_uptime{device=\"cr1\"}"
curl -sG "localhost:${LOKI_PORT}/loki/api/v1/query_range" --data-urlencode 'query={device="cr1"}'
make state DEV=cr1
make check DEV=cr1          # assurance rules
```

## gNMI (optional)

IOS-XE supports gNMI, and this is the only platform in the stack where it is an
option:

```
gnxi
gnxi server
gnxi port 57400
```

```yaml
    telemetry_mode: gnmi
```

SNMP and gNMI are mutually exclusive per device — the renderer will not put a
device in both.
