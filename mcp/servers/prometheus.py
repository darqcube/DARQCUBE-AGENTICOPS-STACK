"""mcp-prometheus — what IS happening, as metrics."""
from __future__ import annotations

import os

from common import Backend, build, duration, identifier, limit

api = Backend(os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
mcp = build("prometheus")

# No raw-PromQL tool. Each tool below builds a bounded query from validated
# arguments, so the blast radius of a bad call is a bad answer, not a dead TSDB.


def _query(promql: str) -> list[dict]:
    body = api.get("/api/v1/query", params={"query": promql})
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus: {body.get('error', 'query failed')}")
    return body["data"]["result"]


def _rows(result: list[dict], value_name: str = "value") -> list[dict]:
    return [
        {**r["metric"], value_name: float(r["value"][1])}
        for r in result
        if "value" in r
    ]


@mcp.tool()
def get_device_metrics(device: str) -> dict:
    """CPU, memory and uptime for one device.

    Args:
        device: the device name as labelled in the source of truth.
    """
    identifier(device, "device")
    sel = f'{{device="{device}"}}'
    return {
        "device": device,
        "cpu_percent": _rows(_query(f"device:cpu_usage:max{sel}")),
        "memory_percent": _rows(_query(f"device:memory_used_percent:max{sel}")),
        "uptime_seconds": _rows(_query(f"device_uptime{sel}")),
    }


@mcp.tool()
def get_interface_status(device: str) -> dict:
    """Every interface on a device, with admin and operational state.

    Status values follow IF-MIB: 1 = up, 2 = down.

    Args:
        device: the device name.
    """
    identifier(device, "device")
    oper = _rows(_query(f'interface_oper_status{{device="{device}"}}'), "oper")
    admin = {
        (r["ifName"]): r["admin"]
        for r in _rows(_query(f'interface_admin_status{{device="{device}"}}'), "admin")
    }
    interfaces = [
        {
            "name": r.get("ifName"),
            "description": r.get("ifAlias", ""),
            "type": r.get("ifType"),
            "oper_up": r["oper"] == 1,
            "admin_up": admin.get(r.get("ifName")) == 1,
        }
        for r in oper
    ]
    return {
        "device": device,
        "count": len(interfaces),
        "down": [i for i in interfaces if i["admin_up"] and not i["oper_up"]],
        "interfaces": interfaces,
    }


@mcp.tool()
def list_alerts(state: str = "firing") -> dict:
    """Currently firing (or pending) alerts across the network.

    Args:
        state: "firing" or "pending".
    """
    if state not in ("firing", "pending"):
        raise ValueError('state must be "firing" or "pending"')
    rows = _rows(_query(f'ALERTS{{alertstate="{state}"}}'))
    return {
        "state": state,
        "count": len(rows),
        "alerts": [
            {
                "alert": r.get("alertname"),
                "device": r.get("device"),
                "severity": r.get("severity"),
                "interface": r.get("ifName"),
            }
            for r in rows
        ],
    }


@mcp.tool()
def get_flow_summary(device: str | None = None, window: str = "1h") -> dict:
    """Traffic volume from NetFlow/IPFIX, by protocol.

    Per-flow addresses and ports are deliberately not collected, so this
    answers "how much traffic, of what kind" but NOT "which host is the top
    talker" — that needs a flow store this stack does not run.

    Args:
        device: limit to one device, or omit for the whole network.
        window: time window, e.g. "15m", "6h". Maximum 24h.
    """
    window = duration(window)
    sel = f'{{device="{identifier(device, "device")}"}}' if device else ""
    rows = _rows(_query(f"sum by (device, protocol) (increase(flow_bytes_total{sel}[{window}]))"), "bytes")
    return {
        "window": window,
        "device": device or "all",
        "by_protocol": sorted(rows, key=lambda r: -r["bytes"]),
    }


@mcp.tool()
def get_down_interfaces(max_results: int | None = None) -> dict:
    """Every interface across the network that is admin-up but operationally down.

    Args:
        max_results: cap on returned rows (default 100, maximum 1000).
    """
    n = limit(max_results)
    rows = _rows(_query("interface_oper_status == 2 and interface_admin_status == 1"))
    return {
        "count": len(rows),
        "interfaces": [
            {
                "device": r.get("device"),
                "site": r.get("site"),
                "interface": r.get("ifName"),
                "description": r.get("ifAlias", ""),
            }
            for r in rows[:n]
        ],
    }
