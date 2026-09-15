"""mcp-grafana — where a human should look."""
from __future__ import annotations

import base64
import os

from common import Backend, build, identifier

URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_PASSWORD", "")

_basic = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
api = Backend(URL, headers={"Authorization": f"Basic {_basic}"})
mcp = build("grafana")


@mcp.tool()
def list_dashboards() -> dict:
    """List the available Grafana dashboards."""
    results = api.get("/api/search", params={"type": "dash-db"})
    return {
        "count": len(results),
        "dashboards": [
            {"title": d["title"], "uid": d["uid"], "folder": d.get("folderTitle", "General")}
            for d in results
        ],
    }


@mcp.tool()
def get_dashboard_link(uid: str, device: str | None = None) -> dict:
    """Build a link to a dashboard, optionally scoped to one device.

    Use this to hand a person somewhere to look rather than describing numbers.

    Args:
        uid: the dashboard uid from list_dashboards.
        device: optional device to pre-select in the dashboard's variable.
    """
    identifier(uid, "uid")
    url = f"{URL}/d/{uid}"
    if device:
        identifier(device, "device")
        url += f"?var-device={device}"
    return {"uid": uid, "device": device, "url": url}
