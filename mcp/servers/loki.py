"""mcp-loki — what happened, as logs."""
from __future__ import annotations

import os
import time

from common import Backend, build, duration, identifier, limit

api = Backend(os.environ.get("LOKI_URL", "http://loki:3100"))
mcp = build("loki")

# No raw-LogQL tool. Each query below is composed from a validated device label
# and a literal search term, with the window and result count capped.


def _range(window: str, count: int, selector: str, term: str | None = None):
    now = time.time_ns()
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[window[-1]] * int(window[:-1])
    query = selector
    if term:
        # Escaped and passed as a literal filter — never interpolated as LogQL.
        query += ' |= "%s"' % term.replace("\\", "\\\\").replace('"', '\\"')
    body = api.get(
        "/loki/api/v1/query_range",
        params={
            "query": query,
            "start": now - seconds * 1_000_000_000,
            "end": now,
            "limit": count,
            "direction": "backward",
        },
    )
    entries = []
    for stream in body.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            entries.append(
                {
                    "time": int(ts) // 1_000_000_000,
                    "device": labels.get("device"),
                    "severity": labels.get("severity"),
                    "message": line,
                }
            )
    entries.sort(key=lambda e: -e["time"])
    return entries


@mcp.tool()
def search_device_logs(
    device: str, term: str | None = None, window: str = "1h", max_results: int | None = None
) -> dict:
    """Search one device's syslog.

    Args:
        device: the device name as labelled in the source of truth.
        term: optional literal substring to match. Not a regex or a query.
        window: time window, e.g. "15m", "6h". Maximum 24h.
        max_results: cap on returned lines (default 100, maximum 1000).
    """
    identifier(device, "device")
    window = duration(window)
    count = limit(max_results)
    entries = _range(window, count, f'{{device="{device}"}}', term)
    return {"device": device, "window": window, "count": len(entries), "logs": entries}


@mcp.tool()
def get_recent_errors(window: str = "1h", max_results: int | None = None) -> dict:
    """Error-and-worse log lines from every device.

    Severity comes from the syslog priority, so this covers emergency, alert,
    critical and error.

    Args:
        window: time window, e.g. "15m", "6h". Maximum 24h.
        max_results: cap on returned lines (default 100, maximum 1000).
    """
    window = duration(window)
    count = limit(max_results)
    selector = '{severity=~"emergency|alert|critical|error"}'
    entries = _range(window, count, selector)
    return {"window": window, "count": len(entries), "logs": entries}


@mcp.tool()
def count_log_pattern(term: str, window: str = "1h") -> dict:
    """Count how many times a literal string appears in logs, per device.

    Useful for "is this happening everywhere or just on one box".

    Args:
        term: literal substring to count. Not a regex.
        window: time window, e.g. "15m", "6h". Maximum 24h.
    """
    window = duration(window)
    safe = term.replace("\\", "\\\\").replace('"', '\\"')
    body = api.get(
        "/loki/api/v1/query",
        params={"query": f'sum by (device) (count_over_time({{device=~".+"}} |= "{safe}" [{window}]))'},
    )
    rows = [
        {"device": r["metric"].get("device"), "count": int(float(r["value"][1]))}
        for r in body.get("data", {}).get("result", [])
    ]
    return {"term": term, "window": window, "by_device": sorted(rows, key=lambda r: -r["count"])}
