"""mcp-grafana — where a human should look, and (optionally) a picture of it.

render_interface_graph is registered only when MCP_GRAPHS_ENABLED=true
(ai_platform.graphs in site.yml), which also starts Grafana's image renderer.
It renders ONE fixed panel of the Interface Detail dashboard for a validated
device and interface — never an arbitrary dashboard, panel or query — keeps
the PNG briefly in memory, and returns a link the chat shows as an image.
"""
from __future__ import annotations

import base64
import logging
import os
import urllib.parse

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from common import Backend, BoundsError, build, duration, identifier, interface_name
from images import ImageStore

URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
# Where a PERSON reaches Grafana. URL above is the compose-network name, which
# only containers can resolve; a link built from it opens nothing in a browser.
PUBLIC_URL = (os.environ.get("GRAFANA_PUBLIC_URL") or URL).rstrip("/")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_PASSWORD", "")

GRAPHS = os.environ.get("MCP_GRAPHS_ENABLED", "false").lower() == "true"
# Where a person's browser reaches THIS server, for image links.
IMAGE_BASE_URL = os.environ.get("MCP_IMAGE_BASE_URL", "").rstrip("/")
IMAGE_TTL = int(os.environ.get("MCP_IMAGE_TTL_SECONDS") or 900)
GRAPH_TZ = os.environ.get("MCP_GRAPH_TZ") or "UTC"
MAX_IMAGE_BYTES = 2 * 1024 * 1024

# The one dashboard graphs are rendered from, and the panel per kind. Fixed:
# a caller picks a kind, never a dashboard or panel id.
GRAPH_DASHBOARD = ("darqcube-interface", "interface-detail")
GRAPH_PANELS = {"traffic": 1, "errors": 2}

_basic = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
api = Backend(URL, headers={"Authorization": f"Basic {_basic}"})
# A render drives a headless browser: seconds, not milliseconds.
render_api = Backend(URL, headers={"Authorization": f"Basic {_basic}"}, timeout=60.0)
prometheus = Backend(os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
IMAGES = ImageStore(ttl_seconds=IMAGE_TTL)
mcp = build("grafana")
log = logging.getLogger("mcp.audit")


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
def get_dashboard_link(uid: str, device: str | None = None, interface: str | None = None) -> dict:
    """Build a link to a dashboard, optionally scoped to one device and interface.

    Use this to hand a person somewhere to look rather than describing numbers.
    For one interface's graphs, use uid "darqcube-interface".

    Args:
        uid: the dashboard uid from list_dashboards.
        device: optional device to pre-select in the dashboard's variable.
        interface: optional interface name (e.g. "Et0/1") to pre-select.
    """
    url = _dashboard_url(uid, device, interface)
    return {"uid": uid, "device": device, "interface": interface, "url": url}


def _dashboard_url(uid: str, device: str | None = None, interface: str | None = None) -> str:
    # A plain helper: a registered tool is replaced by its async wrapper, so
    # tools must not call each other directly.
    identifier(uid, "uid")
    params = {}
    if device:
        params["var-device"] = identifier(device, "device")
    if interface:
        params["var-interface"] = interface_name(interface)
    return f"{PUBLIC_URL}/d/{uid}" + (f"?{urllib.parse.urlencode(params)}" if params else "")


def _bps(value: float) -> str:
    for unit, scale in (("Gbps", 1e9), ("Mbps", 1e6), ("kbps", 1e3)):
        if value >= scale:
            return f"{value / scale:.2f} {unit}"
    return f"{value:.0f} bps"


def _scalar(promql: str) -> float | None:
    body = prometheus.get("/api/v1/query", params={"query": promql})
    result = (body.get("data") or {}).get("result") or []
    return float(result[0]["value"][1]) if result else None


def _summary(device: str, interface: str, window: str, kind: str) -> dict:
    """The numbers behind the picture — so the answer has them even if the
    image never loads, and the model can talk about them."""
    sel = f'{{device="{device}", ifName="{interface}"}}'
    metric = {"traffic": "interface_{d}_octets", "errors": "interface_{d}_errors"}[kind]
    scale = " * 8" if kind == "traffic" else ""
    out = {}
    for direction in ("in", "out"):
        # sum(): one number even if the series' labels changed in the window —
        # taking the first of several series reported 0 next to a busy graph.
        rate = f"sum(rate({metric.format(d=direction)}{sel}[5m])){scale}"
        stats = {
            "now": _scalar(rate),
            "avg": _scalar(f"avg_over_time(({rate})[{window}:1m])"),
            "max": _scalar(f"max_over_time(({rate})[{window}:1m])"),
        }
        if kind == "traffic":
            stats = {k: (_bps(v) if v is not None else "no data") for k, v in stats.items()}
        else:
            stats = {k: (round(v, 3) if v is not None else "no data") for k, v in stats.items()}
        out[direction] = stats
    return out


if GRAPHS:

    @mcp.tool()
    def render_interface_graph(device: str, interface: str, window: str = "1h",
                               kind: str = "traffic") -> dict:
        """Render a graph of one interface and return it as an image link for the chat.

        Put the returned `markdown` in your answer EXACTLY as given — it shows
        the graph to the user. Never write or alter an image URL yourself.
        The `summary` holds the same numbers as text.

        Args:
            device: the device name as it appears in the source of truth.
            interface: the interface name, e.g. "Et0/1" (see get_interface_status).
            window: how far back, e.g. "15m", "1h", "6h". Maximum 24h.
            kind: "traffic" (bits per second in and out) or "errors" (errors per second).
        """
        identifier(device, "device")
        interface_name(interface)
        window = duration(window)
        if kind not in GRAPH_PANELS:
            raise BoundsError(f"kind must be one of {sorted(GRAPH_PANELS)}")
        if not IMAGE_BASE_URL:
            raise RuntimeError("image links are not configured: set MCP_IMAGE_BASE_URL "
                               "(ai_platform.publish in site.yml derives it)")

        uid, slug = GRAPH_DASHBOARD
        png = render_api.get_bytes(
            f"/render/d-solo/{uid}/{slug}",
            params={
                "orgId": 1, "panelId": GRAPH_PANELS[kind],
                "var-device": device, "var-interface": interface,
                "from": f"now-{window}", "to": "now",
                "width": 1000, "height": 450, "theme": "dark", "tz": GRAPH_TZ,
            },
            max_bytes=MAX_IMAGE_BYTES, content_type="image/png",
        )
        image_id = IMAGES.put(png, label=f"{device} {interface} {kind} {window}")
        url = f"{IMAGE_BASE_URL}/g/{image_id}.png"
        alt = f"{device} {interface} {kind}, last {window}"
        link = _dashboard_url("darqcube-interface", device, interface)
        return {
            "markdown": f"![{alt}]({url})",
            "image_url": url,
            "alt": alt,
            "expires_in_seconds": IMAGE_TTL,
            "summary": _summary(device, interface, window, kind),
            "dashboard_url": link,
        }


async def _image(request: Request) -> Response:
    """GET /g/<id>.png — the link IS the authorisation (see images.py)."""
    name = request.path_params["name"]
    found = IMAGES.get(name[:-4]) if name.endswith(".png") else None
    if not found:
        return PlainTextResponse("not found or expired", status_code=404)
    data, content_type = found
    log.info("audit image served server=grafana id=%s…", name[:6])
    return Response(data, media_type=content_type, headers={
        "Cache-Control": f"private, max-age={IMAGE_TTL}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
    })


PUBLIC_ROUTES = [Route("/g/{name}", _image, methods=["GET"])] if GRAPHS else []
