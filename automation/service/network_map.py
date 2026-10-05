"""Network map topology for the Grafana Network Map dashboard.

The ESnet Network Map Panel draws a topology it is given as JSON and colours
its edges from query data. Its "autodetect from data" mode would build the
topology from the query, but (v3.1.0) it rebuilds every edge on each render
and drops the traffic colours — so the topology comes from a file instead:

    devices    intent_device_present — every active Infrahub device, at its
               map position (lat/lng labels, from `make render`)
    links      link:info — one series per link: LLDP (physical) and BGP
               (routing adjacencies, e.g. over GRE tunnels)

Every MAP_INTERVAL_SECONDS this writes MAP_OUTPUT in the panel's "load config
from one URL" format. Grafana serves that folder at
/public/darqcube-map/, and the panel's query colours the edges live.
The file is only rewritten when the topology changes. Also served at
GET /map/topology for inspection.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
import urllib.parse
import urllib.request

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
INTERVAL = float(os.environ.get("MAP_INTERVAL_SECONDS", "60") or 0)
OUTPUT = os.environ.get("MAP_OUTPUT", "/map/network-map.json")
MARGIN = 1.5   # degrees of space around the outermost devices

_started = False


def _query(expr: str) -> list[dict]:
    url = f"{PROMETHEUS_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)["data"]["result"]


def _node_svg(name: str) -> str:
    """The panel draws a bare circle per node; this adds the device name."""
    safe = html.escape(name, quote=True)
    return (f"<circle r='7'></circle>"
            f"<text x='10' y='4' style='font: 12px sans-serif; fill: #1f1f1f; stroke: none'>{safe}</text>")


def build(devices: list[dict], links: list[dict]) -> dict:
    """Panel configuration from intent_device_present and link:info samples."""
    nodes: dict[str, dict] = {}
    for s in devices:
        m = s["metric"]
        try:
            coord = [float(m["lat"]), float(m["lng"])]
        except (KeyError, ValueError):
            continue          # rendered before map positions existed: re-run make render
        nodes[m["device"]] = {"name": m["device"], "coordinate": coord,
                              "meta": {"display_name": m["device"], "svg": _node_svg(m["device"]), "template": ""}}
    edges: dict[str, dict] = {}
    for s in links:
        a, z = s["metric"]["device"], s["metric"]["neighbor"]
        if a not in nodes or z not in nodes or f"{z}--{a}" in edges:
            continue
        (la, ga), (lz, gz) = nodes[a]["coordinate"], nodes[z]["coordinate"]
        dist = ((la - lz) ** 2 + (ga - gz) ** 2) ** 0.5
        # A control point slightly off the straight line, so parallel paths
        # (several tunnels from one branch) fan out instead of overlapping.
        mid = [round((la + lz) / 2 + 0.08 * dist, 4), round((ga + gz) / 2, 4)]
        edges[f"{a}--{z}"] = {"name": f"{a}--{z}", "coordinates": [[la, ga], mid, [lz, gz]],
                              "meta": {"endpoint_identifiers": {"names": [a, z]}}, "children": []}
    topology = {"nodes": sorted(nodes.values(), key=lambda n: n["name"]),
                "edges": sorted(edges.values(), key=lambda e: e["name"])}
    config: dict = {"layers": [{"mapjson": json.dumps(topology)},
                               {"mapjson": '{"edges":[],"nodes":[]}'},
                               {"mapjson": '{"edges":[],"nodes":[]}'}]}
    if nodes:
        lats = [n["coordinate"][0] for n in nodes.values()]
        lngs = [n["coordinate"][1] for n in nodes.values()]
        config["viewport"] = {"top": max(lats) + MARGIN, "bottom": min(lats) - MARGIN,
                              "left": min(lngs) - MARGIN, "right": max(lngs) + MARGIN,
                              "center": {"lat": (max(lats) + min(lats)) / 2, "lng": (max(lngs) + min(lngs)) / 2},
                              "zoom": 5}
    return config


def current() -> dict:
    return build(_query("intent_device_present"), _query("link:info"))


def write_once() -> bool:
    """Write OUTPUT if the topology changed. Returns whether it wrote."""
    body = json.dumps(current(), indent=1, sort_keys=True)
    try:
        with open(OUTPUT) as fh:
            if fh.read() == body:
                return False
    except FileNotFoundError:
        pass
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o644)          # Grafana reads it as another user
    os.replace(tmp, OUTPUT)       # atomic: the panel never fetches half a file
    return True


def _loop() -> None:
    while True:
        try:
            write_once()
        except Exception as exc:  # Prometheus restarting, folder not mounted
            print(f"network map: {exc}", flush=True)
        time.sleep(max(15.0, INTERVAL))


def start() -> bool:
    global _started
    if INTERVAL <= 0 or _started or not os.path.isdir(os.path.dirname(OUTPUT)):
        return _started
    _started = True
    threading.Thread(target=_loop, name="network-map", daemon=True).start()
    return True
