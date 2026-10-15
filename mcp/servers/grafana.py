"""mcp-grafana — where a human should look, and (optionally) a picture of it.

render_interface_graph is registered only when MCP_GRAPHS_ENABLED=true
(ai_platform.graphs in site.yml), which also starts Grafana's image renderer.
It renders ONE fixed panel of the Interface Detail dashboard for a validated
device and interface — never an arbitrary dashboard, panel or query — keeps
the PNG briefly in memory, and returns a link the chat shows as an image.

Renders are a shared, slow resource (a headless browser, ~6 s each): they
queue for a fixed number of slots, identical requests within a minute share
one render, and Grafana's own "could not render" images are recognised and
never shown as a graph.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
import urllib.parse

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from common import Backend, BoundsError, build, duration, identifier, interface_name
from images import ID as IMAGE_ID, ImageStore, RecentRenders

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

# Two groups of links side by side, picked by interface description (ifAlias)
# and by the IP SLA probes tagged with that same description. Fixed like the
# above: traffic_share renders Link Share whole (its two pies), the others one
# panel of Link SLA.
SHARE_DASHBOARD = ("darqcube-link-share", "link-share")
SLA_DASHBOARD = ("darqcube-link-sla", "link-sla")
SLA_PANELS = {"availability": 1, "rtt": 2, "jitter": 3}
# The numbers behind each kind, per device, for one link name — the same
# queries as the dashboards, with the window for $__range.
COMPARE_QUERIES = {
    "traffic_share": ('sum by (device) (rate(interface_in_octets{{ifAlias="{link}"}}[{w}])'
                      ' + rate(interface_out_octets{{ifAlias="{link}"}}[{w}])) * 8'),
    "availability": 'avg by (device) (avg_over_time(ipsla:ok{{probe="{link}"}}[{w}])) * 100',
    "rtt": ('avg by (device) (avg_over_time((ipsla_rtt_ms{{probe="{link}"}}'
            ' and on (device, index) ipsla_sense == 1)[{w}:1m]))'),
    "jitter": 'avg by (device) (stddev_over_time(ipsla_rtt_ms{{probe="{link}", rtt_type="1"}}[{w}]))',
}

# Render slots: equal to Grafana's GF_RENDERING_CONCURRENT_RENDER_REQUEST_LIMIT
# (compose sets both; test_render pins them together). Over the limit, Grafana
# does not queue — it answers 200 with a "limit reached" picture.
RENDER_SLOTS = int(os.environ.get("MCP_RENDER_LIMIT") or 4)
# How long a request waits for a slot. The ai-platform's MCP client reads a
# tool result for up to 5 minutes; a slot frees every ~6 s under load.
QUEUE_WAIT = 60.0
# Waits before retrying a "limit reached" picture — another Grafana user (a
# person sharing a panel image) took the slot this server thought was free.
BUSY_BACKOFF = (2.0, 4.0, 8.0)
# Identical requests within this window share one render.
GRAPH_CACHE_SECONDS = 60

# Grafana answers a render it cannot do with one of its own static images —
# HTTP 200, image/png, exactly the file's bytes (verified on 12.0). Fetched
# from Grafana itself, so they match the running version.
PLACEHOLDERS = {
    "busy": ("rendering_limit_dark", "rendering_limit_light"),
    "failed": ("rendering_error_dark", "rendering_error_light", "rendering_timeout_dark",
               "rendering_timeout_light", "rendering_plugin_not_installed"),
}

_basic = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
api = Backend(URL, headers={"Authorization": f"Basic {_basic}"})
# A render drives a headless browser: seconds, not milliseconds.
render_api = Backend(URL, headers={"Authorization": f"Basic {_basic}"}, timeout=60.0)
prometheus = Backend(os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"))
IMAGES = ImageStore(ttl_seconds=IMAGE_TTL)
RECENT = RecentRenders(ttl_seconds=GRAPH_CACHE_SECONDS)
mcp = build("grafana")
log = logging.getLogger("mcp.audit")

_slots = threading.BoundedSemaphore(RENDER_SLOTS)
# One render per request at a time, so identical requests wait for the first
# and then read RECENT. Striped, not per key: nothing to clean up, and two
# different requests share a stripe rarely.
_key_locks = [threading.Lock() for _ in range(64)]
_placeholders: dict[str, str] = {}      # sha256 of the image -> "busy" | "failed"
_placeholders_lock = threading.Lock()


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


def _per_device(promql: str) -> dict[str, float]:
    body = prometheus.get("/api/v1/query", params={"query": promql})
    result = (body.get("data") or {}).get("result") or []
    return {s["metric"].get("device", "?"): float(s["value"][1]) for s in result}


def _compare_summary(link_a: str, link_b: str, window: str, kind: str) -> dict:
    """Per link, per device, in readable units — the numbers behind the picture."""
    out = {}
    for link in (link_a, link_b):
        values = _per_device(COMPARE_QUERIES[kind].format(link=link, w=window))
        total = sum(values.values())
        if kind == "traffic_share":
            out[link] = {d: {"traffic": _bps(v), "share": f"{100 * v / total:.1f} %" if total else "0 %"}
                         for d, v in sorted(values.items())}
        elif kind == "availability":
            out[link] = {d: f"{v:.2f} %" for d, v in sorted(values.items())}
        else:
            out[link] = {d: f"{v:.1f} ms" for d, v in sorted(values.items())}
    return out


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


class GraphUnavailable(Exception):
    """No graph this time; the tool still returns the numbers."""


def _placeholder(png: bytes) -> str | None:
    """"busy" or "failed" if this is one of Grafana's own error pictures."""
    with _placeholders_lock:
        if not _placeholders:
            for kind, names in PLACEHOLDERS.items():
                for name in names:
                    try:
                        image = render_api.get_bytes(f"/public/img/{name}.png", max_bytes=MAX_IMAGE_BYTES,
                                                     content_type="image/png")
                    except RuntimeError:
                        continue        # not in this Grafana version
                    _placeholders[hashlib.sha256(image).hexdigest()] = kind
            if not _placeholders:
                # Retried on the next render; until then an error picture
                # would pass as a graph, so say so.
                log.warning("audit graph placeholders unavailable server=grafana")
    return _placeholders.get(hashlib.sha256(png).hexdigest())


def _render_png(path: str, params: dict) -> bytes:
    """One render, through a slot. Raises GraphUnavailable, never returns an
    error picture."""
    for wait in (*BUSY_BACKOFF, None):
        if not _slots.acquire(timeout=QUEUE_WAIT):
            raise GraphUnavailable(f"every render slot was busy for {QUEUE_WAIT:.0f} s; "
                                   "ask again in a minute")
        try:
            png = render_api.get_bytes(path, params=params,
                                       max_bytes=MAX_IMAGE_BYTES, content_type="image/png")
        finally:
            _slots.release()
        found = _placeholder(png)
        if found is None:
            return png
        if found == "failed":
            raise GraphUnavailable("Grafana could not render the panel")
        if wait is None:
            raise GraphUnavailable("Grafana's renderer stayed at its limit; ask again in a minute")
        log.info("audit render busy server=grafana retry_in=%ss", wait)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _graph_png(key: tuple, path: str, params: dict) -> bytes:
    with _key_locks[hash(key) % len(_key_locks)]:
        png = RECENT.get(key)
        if png is None:
            png = _render_png(path, params)
            RECENT.put(key, png)
        return png


def _image_result(png: bytes, label: str, alt: str, summary: dict, link: str) -> dict:
    image_id = IMAGES.put(png, label=label)
    url = f"{IMAGE_BASE_URL}/g/{image_id}.png"
    return {
        "markdown": f"![{alt}]({url})",
        "image_url": url,
        "alt": alt,
        "expires_in_seconds": IMAGE_TTL,
        "summary": summary,
        "dashboard_url": link,
    }


def _unavailable(exc: GraphUnavailable, what: str, alt: str, summary: dict, link: str) -> dict:
    log.warning("audit graph unavailable server=grafana %s reason=%s", what, exc)
    # No markdown: a picture of an error must never pass as a graph.
    return {"graph": "unavailable", "reason": str(exc), "alt": alt, "summary": summary,
            "dashboard_url": link}


if GRAPHS:

    @mcp.tool()
    def render_interface_graph(device: str, interface: str, window: str = "1h",
                               kind: str = "traffic") -> dict:
        """Render a graph of one interface and return it as an image link for the chat.

        Put the returned `markdown` in your answer EXACTLY as given — it shows
        the graph to the user. Never write or alter an image URL yourself.
        The link expires after 15 minutes: call this tool again for EVERY graph
        request, even one already shown earlier in the chat — never reuse an
        old image link. The `summary` holds the same numbers as text.
        If the result has no `markdown`, the graph could not be drawn: say
        so with its `reason`, and give the `summary` and `dashboard_url`.

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

        params = {
            "orgId": 1, "panelId": GRAPH_PANELS[kind],
            "var-device": device, "var-interface": interface,
            "from": f"now-{window}", "to": "now",
            "width": 1000, "height": 450, "theme": "dark", "tz": GRAPH_TZ,
        }
        alt = f"{device} {interface} {kind}, last {window}"
        link = _dashboard_url("darqcube-interface", device, interface)
        uid, slug = GRAPH_DASHBOARD
        try:
            png = _graph_png((device, interface, window, kind), f"/render/d-solo/{uid}/{slug}", params)
        except GraphUnavailable as exc:
            return _unavailable(exc, f"device={device} interface={interface}", alt,
                                _summary(device, interface, window, kind), link)
        return _image_result(png, f"{device} {interface} {kind} {window}", alt,
                             _summary(device, interface, window, kind), link)

    @mcp.tool()
    def render_link_comparison(link_a: str, link_b: str, kind: str = "traffic_share",
                               window: str = "24h") -> dict:
        """Compare two groups of links side by side — e.g. the links to two
        providers — as one image link for the chat.

        A group is every interface whose description (ifAlias) is that name,
        and every IP SLA probe tagged with the same name. Put the returned
        `markdown` in your answer EXACTLY as given; never write or alter an
        image URL. The link expires after 15 minutes: call again for EVERY
        request. The `summary` holds the numbers per link and device. If the
        result has no `markdown`, the graph could not be drawn: say so with
        its `reason`, and give the `summary` and `dashboard_url`.

        Args:
            link_a: interface description of the first group, e.g. "To-Provider-A".
            link_b: interface description of the second group, e.g. "To-Provider-B".
            kind: "traffic_share" (two pie charts: each device's share of each
                group's traffic), or the IP SLA of both groups as grouped bars per
                device: "availability" (% of probes that succeeded), "rtt"
                (average round-trip ms), "jitter" (spread of round-trip ms).
            window: how far back, e.g. "1h", "6h", "24h". Maximum 24h.
        """
        identifier(link_a, "link_a")
        identifier(link_b, "link_b")
        window = duration(window, default="24h")
        if kind not in COMPARE_QUERIES:
            raise BoundsError(f"kind must be one of {sorted(COMPARE_QUERIES)}")
        if not IMAGE_BASE_URL:
            raise RuntimeError("image links are not configured: set MCP_IMAGE_BASE_URL "
                               "(ai_platform.publish in site.yml derives it)")

        summary = _compare_summary(link_a, link_b, window, kind)
        if not any(summary.values()):
            what = "interface description" if kind == "traffic_share" else "IP SLA probe tag"
            raise BoundsError(f"no {what} is {link_a!r} or {link_b!r} in the last {window}; "
                              "use the exact description (ifAlias) — see get_interface_status")

        params = {
            "orgId": 1, "var-link_a": link_a, "var-link_b": link_b,
            "from": f"now-{window}", "to": "now", "theme": "dark", "tz": GRAPH_TZ,
        }
        if kind == "traffic_share":
            uid, slug = SHARE_DASHBOARD
            # The whole dashboard — its two pies — without Grafana's chrome.
            path = f"/render/d/{uid}/{slug}"
            params.update({"width": 1000, "height": 480, "kiosk": "1", "_dash.hideTimePicker": "",
                           "_dash.hideVariables": "", "_dash.hideLinks": ""})
        else:
            uid, slug = SLA_DASHBOARD
            path = f"/render/d-solo/{uid}/{slug}"
            params.update({"panelId": SLA_PANELS[kind], "width": 1000, "height": 420})

        alt = f"{link_a} vs {link_b} {kind.replace('_', ' ')}, last {window}"
        query = urllib.parse.urlencode({"var-link_a": link_a, "var-link_b": link_b,
                                        "from": f"now-{window}", "to": "now"})
        link = f"{PUBLIC_URL}/d/{uid}?{query}"
        try:
            png = _graph_png(("compare", link_a, link_b, kind, window), path, params)
        except GraphUnavailable as exc:
            return _unavailable(exc, f"link_a={link_a} link_b={link_b}", alt, summary, link)
        return _image_result(png, f"{link_a} vs {link_b} {kind} {window}", alt, summary, link)


# Shown in place of a graph whose link has expired (IMAGE_TTL passed, or the
# server restarted — the store is memory only). A bare 404 left a broken image
# nobody could explain, typically after a model re-used a link from earlier in
# the chat. Static: nothing from the request is reflected into it.
EXPIRED_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="120" viewBox="0 0 1000 120">'
    '<rect width="1000" height="120" rx="8" fill="#1f2937"/>'
    '<text x="32" y="52" font-family="sans-serif" font-size="24" fill="#f3f4f6">'
    'Graph expired</text>'
    '<text x="32" y="88" font-family="sans-serif" font-size="18" fill="#9ca3af">'
    'Graph links last a short while. Ask again for a fresh graph.</text></svg>'
).encode()

_LOCKED_DOWN = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "Referrer-Policy": "no-referrer",
}


async def _image(request: Request) -> Response:
    """GET /g/<id>.png — the link IS the authorisation (see images.py)."""
    name = request.path_params["name"]
    image_id = name[:-4] if name.endswith(".png") else ""
    if not IMAGE_ID.fullmatch(image_id):
        return PlainTextResponse("not found", status_code=404)
    found = IMAGES.get(image_id)
    if not found:
        # 200, not 404: an <img> shows the body of a successful response only.
        log.info("audit image expired server=grafana id=%s…", name[:6])
        return Response(EXPIRED_SVG, media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store", **_LOCKED_DOWN})
    data, content_type = found
    log.info("audit image served server=grafana id=%s…", name[:6])
    return Response(data, media_type=content_type, headers={
        "Cache-Control": f"private, max-age={IMAGE_TTL}", **_LOCKED_DOWN})


PUBLIC_ROUTES = [Route("/g/{name}", _image, methods=["GET"])] if GRAPHS else []
