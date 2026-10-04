# NetFlow from exporters behind NAT

Flow records are labelled by **who sent them**: `processors.lookup` matches the
packet's source address against the identity table that `make render` writes
from Infrahub. That works whenever the stack sees each device's own address.

It breaks when exporters reach the stack **through NAT** — a container lab on a
laptop, a remote site behind a firewall, devices in a cloud VPC. Every exporter
then arrives from the same translated address, the lookup cannot tell them
apart, and flows land with no `device`, `site` or `role` (or, worse, all on one
device if the NAT address happens to match a record).

## The fix: a dedicated port per exporter

When the source address cannot identify a device, the **destination port** can.
Give the device its own listener:

1. In Infrahub (or `devices.yml`), set `flow_enabled: true` and a `flow_port`
   inside the published range:

   ```yaml
     - name: edge-01
       ...
       flow_enabled: true
       flow_port: 12056      # unique per device, within FLOW_DEDICATED_FIRST..LAST
   ```

2. `make seed && make render`. The renderer writes
   `generated/netflow-dedicated.conf`, one `[[inputs.netflow]]` per device on its
   `flow_port`, tagged `flow_exporter = "<device name>"`.
3. Point the device's exporter at `SYSLOG_COLLECTOR_IP`, **port = its
   `flow_port`** (instead of `NETFLOW_PORT`). Cisco IOS-XE:

   ```
   flow exporter DARQCUBE-EXPORTER
    destination <SYSLOG_COLLECTOR_IP>
    transport udp 12056
    export-protocol netflow-v9
   ```

The identity lookup keys on `flow_exporter` first and falls back to the source
address (`observability/telegraf/conf.d/outputs.conf`), so these records carry the same
`device`/`site`/`role` labels as the device's SNMP metrics and syslog.

## The port range

Compose publishes one contiguous range on the Telegraf container, with the same
number on both sides:

| Variable | Shipped | Standard |
|---|---|---|
| `FLOW_DEDICATED_FIRST` | 12056 | 2056 |
| `FLOW_DEDICATED_LAST` | 12105 | 2105 |

That is 50 exporters. Widen it in `.env` (and the firewall) for more; then
`make up` to republish and `make render` so the renderer validates against the
new bounds.

## What the renderer refuses — loudly

`make render` prints a warning and **does not write** the listener when:

| Condition | Why |
|---|---|
| `flow_port` set but `flow_enabled` false | the record says the device does not export flow |
| two devices share a `flow_port` | records could not be told apart again |
| `flow_port` outside `FLOW_DEDICATED_FIRST..LAST` | compose does not publish it; nothing would ever arrive |

## Devices that do not need this

A device the stack sees directly keeps using the shared `NETFLOW_PORT` (or
`IPFIX_PORT`) and is identified by its `management_ip` — leave `flow_port`
unset. Both kinds can coexist in one deployment.

## Verify

```sh
docker compose exec telegraf cat /etc/telegraf/telegraf.d/generated/netflow-dedicated.conf
sudo tcpdump -ni any udp portrange 12056-12105 -c 5        # packets arriving?
curl -s localhost:${PROMETHEUS_PORT}/api/v1/query \
  --data-urlencode 'query=sum by (device) (netflow_flow_bytes_total)'
```

A flow series with **no** `device` label means its records came in on the
shared port from an address the identity table does not know — that device
needs a `flow_port` (or a correct `management_ip`).

NetFlow v9 records only decode after the exporter's next template refresh
(`template data timeout`, 60 s on Cisco by default), and the collector sums
over 60 s, so allow two minutes before concluding nothing arrives.
