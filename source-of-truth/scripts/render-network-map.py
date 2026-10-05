#!/usr/bin/env python3
"""Render the Network Map dashboard from a drawing of the network.

Reads : source-of-truth/map/*.yml   (the drawing: nodes with positions, links)
        <RENDER_OUT_DIR>/devices.json (written by render-inventory.py: which
                                      nodes are polled devices)
Writes: <DASHBOARD_OUT>/network-map.json (provisioned by Grafana)

The map is a Network Weathermap NG panel (tamirsuliman-weathermap-panel).
That panel keeps its whole topology in the dashboard, so the dashboard is
generated: positions and cabling come from the drawing — a deployment's own
file, like devices.yml — and every link side is bound to the SNMP traffic of
the interface named in the drawing:

    A side (A -> Z)  = polled end's out, or the other end's in
    Z side (Z -> A)  = the opposite
    bandwidth        = the drawing's `bandwidth`, else that interface's ifSpeed

Nodes that are inventory devices are coloured by whether they report. Any
other node (a host, a provider cloud) is drawn but carries no data. The
drawing format: source-of-truth/map/examples/topology.yml.

Run by `make render`, after render-inventory.py.
"""
from __future__ import annotations

import glob
import json
import os
import sys

import yaml

MAP_DIR = os.environ.get("MAP_DIR", "/map")
OUT_DIR = os.environ.get("RENDER_OUT_DIR", "/generated")
DASHBOARD_OUT = os.environ.get("DASHBOARD_OUT", "/grafana-dashboards")
UID = "darqcube-network-map"

PROM = {"type": "prometheus", "uid": "darqcube-prometheus"}
ICON_BASE = "public/plugins/tamirsuliman-weathermap-panel/icons/"
ICONS = {"router": "networking/router", "switch": "networking/switch", "server": "networking/server",
         "pc": "networking/pc", "cloud": "networking/cloud", "firewall": "networking/firewall"}
MARGIN = 70
ROW_PX = 30        # Grafana grid row height

# Query legends: a link side names the series it shows.
OUT, IN, SPEED, STATUS = "{dev} {port} out", "{dev} {port} in", "{dev} {port} speed", "STATUS {dev}"


class DrawingError(ValueError):
    pass


def load_drawing(map_dir: str = MAP_DIR) -> dict | None:
    """Merge every *.yml in the map folder. None when there is no drawing."""
    files = sorted(glob.glob(os.path.join(map_dir, "*.yml")))
    if not files:
        return None
    drawing: dict = {"map": {}, "nodes": [], "links": []}
    for path in files:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        unknown = set(doc) - {"map", "nodes", "links"}
        if unknown:
            raise DrawingError(f"{os.path.basename(path)}: unknown top-level key(s) {sorted(unknown)} (have: map, nodes, links)")
        drawing["map"].update(doc.get("map") or {})
        drawing["nodes"] += doc.get("nodes") or []
        drawing["links"] += doc.get("links") or []
    return drawing


def _end(text: str, names: set[str], where: str) -> tuple[str, str]:
    node, sep, port = str(text).partition(":")
    if not sep or not port:
        raise DrawingError(f"{where}: '{text}' must be <node>:<interface>")
    if node not in names:
        raise DrawingError(f"{where}: node '{node}' is not in nodes")
    return node, port


def _bandwidth(value) -> float | None:
    if value in (None, ""):
        return None
    s = str(value).strip().upper()
    mult = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}.get(s[-1:], 1)
    try:
        return float(s[:-1] if mult != 1 else s) * mult
    except ValueError as exc:
        raise DrawingError(f"bandwidth '{value}' is not a number like 1G or 100M") from exc


def weathermap(drawing: dict, polled: set[str]) -> dict:
    """The panel's `weathermap` option: nodes, links, colour scale, settings."""
    names = [str(n.get("name", "")) for n in drawing["nodes"]]
    if "" in names or len(set(names)) != len(names):
        raise DrawingError("every node needs a unique name")
    # scale: one factor, or {x, y}. The panel fits the drawing to its width,
    # so only y spreads things out: a taller drawing gets a taller panel.
    raw = drawing["map"].get("scale", 1.0)
    sx, sy = (float(raw.get("x", 1.0)), float(raw.get("y", 1.0))) if isinstance(raw, dict) else (float(raw), float(raw))
    xs = [float(n["x"]) for n in drawing["nodes"]]
    ys = [float(n["y"]) for n in drawing["nodes"]]
    min_x, min_y = min(xs), min(ys)

    links_raw = []
    for i, l in enumerate(drawing["links"]):
        where = f"links[{i}]"
        a, a_port = _end(l.get("a", ""), set(names), where)
        z, z_port = _end(l.get("z", ""), set(names), where)
        links_raw.append((a, a_port, z, z_port, _bandwidth(l.get("bandwidth"))))
    degree = {n: sum(n in (l[0], l[2]) for l in links_raw) for n in names}

    nodes = {}
    for n in drawing["nodes"]:
        name = str(n["name"])
        icon = ICONS.get(n.get("icon", "router"), n.get("icon", "router"))
        node = {
            "id": f"node-{name}", "label": str(n.get("label", name)), "showLabel": True,
            "position": [round((float(n["x"]) - min_x) * sx + MARGIN), round((float(n["y"]) - min_y) * sy + MARGIN)],
            # every link meets the node at its centre, as in a topology drawing
            "anchors": {str(k): {"numLinks": degree[name] if k == 0 else 0, "numFilledLinks": 0} for k in range(5)},
            "useConstantSpacing": False, "compactVerticalLinks": False,
            "padding": {"vertical": 3, "horizontal": 6},
            "colors": {"font": "#ffffff", "background": "#181b1f", "border": "#181b1f", "statusDown": "#F2495C"},
            "nodeIcon": {"src": ICON_BASE + icon + ".svg", "name": icon, "size": {"width": 36, "height": 36},
                         "padding": {"vertical": 2, "horizontal": 0}, "drawInside": False},
            "isConnection": False,
        }
        if name in polled:
            node["statusQuery"] = STATUS.format(dev=name)
            node["nodeStatusColorTarget"] = "both"
        nodes[name] = node

    links = []
    for i, (a, a_port, z, z_port, bw) in enumerate(links_raw):
        if a in polled:
            src, port, az, za = a, a_port, OUT, IN
        elif z in polled:
            src, port, az, za = z, z_port, IN, OUT
        else:
            src = None
        def side(query_fmt, port_label, direction):
            s = {"bandwidth": bw or 0, "labelOffset": 55, "anchor": 0, "dashboardLink": "",
                 "portLabel": port_label, "portLabelOffset": 14, "portLabelDistance": 10, "directionLabel": direction}
            if src:
                s["query"] = query_fmt.format(dev=src, port=port)
                if not bw:
                    s["bandwidthQuery"] = SPEED.format(dev=src, port=port)
            return s
        links.append({
            "id": f"link-{i}-{a}-{z}",
            "nodes": [nodes[a], nodes[z]],
            "sides": {"A": side(az, a_port, f"{a} → {z}"), "Z": side(za, z_port, f"{z} → {a}")},
            "units": "bps", "arrows": {"width": 7, "height": 9, "offset": 2}, "stroke": 4,
            "showThroughputPercentage": False,
        })

    width = round((max(xs) - min_x) * sx + 2 * MARGIN)
    height = round((max(ys) - min_y) * sy + 2 * MARGIN)
    return {
        "version": 2, "id": "darqcube-network-map",
        "nodes": list(nodes.values()), "links": links,
        "scale": [{"percent": 0, "color": "#73BF69"}, {"percent": 40, "color": "#FADE2A"},
                  {"percent": 60, "color": "#FF9830"}, {"percent": 80, "color": "#F2495C"},
                  {"percent": 95, "color": "#C4162A"}],
        "settings": {
            "link": {"spacing": {"horizontal": 10, "vertical": 5}, "stroke": {"color": "rgba(204, 204, 220, 0.16)"},
                     "label": {"background": "#181b1f", "border": "rgba(204, 204, 220, 0.25)", "font": "rgb(204, 204, 220)"},
                     "showAllWithPercentage": False, "valueMappingMode": "last", "defaultUnits": "bps", "linkDecimals": 1,
                     "dynamicStroke": {"enabled": False, "minWidth": 1, "maxWidth": 10},
                     # speed is seconds per dash cycle: lower is faster
                     "flowAnimation": {"enabled": True, "speed": float(drawing["map"].get("animation_seconds", 1))},
                     "gradientColor": False},
            "fontSizing": {"node": 11, "link": 8},
            "colorScaleMode": "percent",
            "panel": {"backgroundColor": drawing["map"].get("background", "#111217"), "showTimestamp": True,
                      "panelSize": {"width": width, "height": height}, "zoomScale": 0, "offset": {"x": 0, "y": 0},
                      "grid": {"enabled": False, "size": 10, "guidesEnabled": False}},
            "tooltip": {"fontSize": 10, "textColor": "white", "backgroundColor": "black",
                        "inboundColor": "#00cf00", "outboundColor": "#fade2a", "scaleToBandwidth": True},
            "scale": {"position": {"x": 0, "y": 0}, "size": {"width": 60, "height": 160}, "title": "Utilisation",
                      "fontSizing": {"title": 12, "threshold": 10}},
        },
    }


# --- the rest of the dashboard: discovered links (LLDP + BGP) -----------------

SITE = 'group by (device) (intent_device_present{site=~"$site"})'
SITE_N = f'label_replace({SITE}, "neighbor", "$1", "device", "(.*)")'
LINK = "on (device, ifName, neighbor, kind) link:info"


def _link_filter(e):
    # "* 1" drops __name__ so the table can merge A/B/C/D rows on equal labels.
    return f"((({e}) and {LINK}) and on (device) {SITE} or (({e}) and {LINK}) and on (neighbor) {SITE_N}) * 1"


def _stat(pid, title, desc, expr, x, unit="short", red_above_zero=False):
    steps = [{"color": "green", "value": None}, {"color": "red", "value": 1}] if red_above_zero else [{"color": "blue", "value": None}]
    return {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": PROM,
            "gridPos": {"h": 4, "w": 6, "x": x, "y": 0}, "targets": [{"refId": "A", "expr": expr, "instant": True}],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": 0 if unit == "short" else None,
                                         "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"}}


def dashboard(drawing: dict | None, polled: set[str]) -> dict:
    panels = [
        _stat(1, "Links", "Links between inventory devices discovered from LLDP (physical) and BGP (routing adjacencies, e.g. tunnels), each counted once.",
              "network:links:count", 0),
        _stat(2, "Links down", "Discovered links whose interface is down (LLDP) or whose session is not established (BGP).",
              "network:links_down:count", 6, red_above_zero=True),
        _stat(3, "Devices not reporting", "In Infrahub but no SNMP data — drawn red on the map.",
              "network:devices_not_reporting:count", 12, red_above_zero=True),
        _stat(4, "Busiest link now", "Highest one-way rate on any discovered link.",
              "max(link:end:out_bps and on (device, ifName, neighbor, kind) link:info) or vector(0)", 18, unit="bps"),
    ]
    if drawing:
        wm = weathermap(drawing, polled)
        h = max(12, -(-(wm["settings"]["panel"]["panelSize"]["height"] + 50) // ROW_PX))
        panels.append({
            "id": 5, "type": "tamirsuliman-weathermap-panel", "title": "Network map — live link activity",
            "description": ("Drawn from source-of-truth/map/*.yml by make render. Each link is two halves, one per direction, "
                            "coloured by utilisation of the interface's speed; hover for both directions. Devices are red "
                            "when they stop reporting. Edit the drawing, not this panel: make render rewrites it."),
            "datasource": PROM, "gridPos": {"h": h, "w": 24, "x": 0, "y": 4},
            "targets": [
                {"refId": "A", "expr": "sum by (device, ifName) (rate(interface_out_octets[$__rate_interval])) * 8",
                 "legendFormat": OUT.format(dev="{{device}}", port="{{ifName}}"), "range": True},
                {"refId": "B", "expr": "sum by (device, ifName) (rate(interface_in_octets[$__rate_interval])) * 8",
                 "legendFormat": IN.format(dev="{{device}}", port="{{ifName}}"), "range": True},
                {"refId": "C", "expr": "max by (device, ifName) (interface_speed) * 1e6",
                 "legendFormat": SPEED.format(dev="{{device}}", port="{{ifName}}"), "range": True},
                {"refId": "D", "expr": ("(max by (device) (intent_device_present) unless on (device) device:not_reporting)"
                                        " or max by (device) (device:not_reporting) * 0"),
                 "legendFormat": STATUS.format(dev="{{device}}"), "range": True},
            ],
            "options": {"weathermap": wm},
        })
        y = 4 + h
    else:
        panels.append({"id": 5, "type": "text", "title": "Network map", "gridPos": {"h": 6, "w": 24, "x": 0, "y": 4},
                       "options": {"mode": "markdown", "content":
                           "No drawing yet. Copy `source-of-truth/map/examples/topology.yml` to `source-of-truth/map/`, "
                           "place your nodes and links, then run `make render`. See docs/how-to/draw-the-network-map.md."}})
        y = 10
    panels += [
        {"id": 6, "type": "table", "title": "Discovered links — traffic each way",
         "description": "Links found by LLDP (kind lldp) and BGP (kind bgp), each once. A → B leaves the A end's interface. Utilisation is the busier direction against ifSpeed.",
         "datasource": PROM, "gridPos": {"h": 11, "w": 24, "x": 0, "y": y},
         "targets": [{"refId": r, "expr": _link_filter(e), "instant": True, "range": False, "format": "table"} for r, e in (
             ("A", "link:end:out_bps"), ("B", "link:end:in_bps"), ("C", "link:end:up"),
             ("D", "100 * (link:end:out_bps > link:end:in_bps or link:end:in_bps)"
                   " / on (device, ifName) group_left () (max by (device, ifName) (interface_speed) * 1e6 > 0)"))],
         "transformations": [
             {"id": "merge", "options": {}},
             {"id": "organize", "options": {
                 "excludeByName": {"Time": True, "__name__": True},
                 "indexByName": {"device": 0, "ifName": 1, "kind": 2, "neighbor": 3, "neighbor_port": 4,
                                 "Value #C": 5, "Value #A": 6, "Value #B": 7, "Value #D": 8},
                 "renameByName": {"device": "A", "ifName": "A interface", "kind": "Kind", "neighbor": "B", "neighbor_port": "B interface",
                                  "Value #A": "A → B", "Value #B": "B → A", "Value #C": "State", "Value #D": "Utilisation"}}},
             {"id": "sortBy", "options": {"sort": [{"field": "A → B", "desc": True}]}}],
         "fieldConfig": {"defaults": {}, "overrides": [
             {"matcher": {"id": "byRegexp", "options": "A → B|B → A"}, "properties": [{"id": "unit", "value": "bps"}]},
             {"matcher": {"id": "byName", "options": "Utilisation"}, "properties": [
                 {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
                 {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
                 {"id": "min", "value": 0}, {"id": "max", "value": 100},
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "green", "value": None}, {"color": "orange", "value": 70}, {"color": "red", "value": 90}]}}]},
             {"matcher": {"id": "byName", "options": "State"}, "properties": [
                 {"id": "mappings", "value": [{"type": "value", "options": {
                     "1": {"text": "up", "color": "green", "index": 0}, "0": {"text": "down", "color": "red", "index": 1}}}]},
                 {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
             {"matcher": {"id": "byName", "options": "A"}, "properties": [{"id": "links", "value": [
                 {"title": "Open device", "url": "/d/darqcube-devices?var-device=${__value.raw}&${__url_time_range}"}]}]}]},
         "options": {"showHeader": True, "cellHeight": "sm"}},
        {"id": 7, "type": "timeseries", "title": "Busiest links",
         "description": "Top 10 discovered links by traffic, both directions together.",
         "datasource": PROM, "gridPos": {"h": 9, "w": 24, "x": 0, "y": y + 11},
         "targets": [{"refId": "A", "legendFormat": "{{device}} {{ifName}} ↔ {{neighbor}} ({{kind}})",
                      "expr": "topk(10, " + _link_filter("link:end:out_bps + link:end:in_bps") + ")"}],
         "fieldConfig": {"defaults": {"unit": "bps", "custom": {"fillOpacity": 10, "showPoints": "never"}}, "overrides": []},
         "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}}},
    ]
    return {
        "uid": UID, "title": "Network Map", "tags": ["darqcube", "snmp", "topology"],
        "description": "The network as drawn in source-of-truth/map/, live traffic on every link; links discovered from LLDP and BGP below. Generated by make render.",
        "timezone": "browser", "schemaVersion": 39, "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
        "links": [{"title": "DarqCube dashboards", "type": "dashboards", "tags": ["darqcube"], "asDropdown": True}],
        "templating": {"list": [{
            "name": "site", "label": "Site", "type": "query", "datasource": PROM,
            "query": "label_values(intent_device_present, site)", "refresh": 2, "includeAll": True, "multi": True,
            "current": {"text": "All", "value": "$__all"}, "sort": 1,
            "description": "Discovered links with at least one end at these sites."}]},
        "panels": panels,
    }


def polled_devices(out_dir: str = OUT_DIR) -> set[str]:
    try:
        with open(os.path.join(out_dir, "devices.json")) as fh:
            return {rec["device"] for rec in json.load(fh).values()}
    except FileNotFoundError:
        return set()


def main() -> int:
    try:
        drawing = load_drawing()
        dash = dashboard(drawing, polled_devices())
    except (DrawingError, KeyError, TypeError, ValueError) as exc:
        print(f"!! network map: {exc}", file=sys.stderr)
        return 1
    os.makedirs(DASHBOARD_OUT, exist_ok=True)
    path = os.path.join(DASHBOARD_OUT, "network-map.json")
    with open(path, "w") as fh:
        json.dump(dash, fh, indent=1, ensure_ascii=False)
    os.chmod(path, 0o644)   # Grafana reads it as another user
    if drawing:
        print(f"    map   network-map.json  {len(drawing['nodes'])} node(s), {len(drawing['links'])} link(s)")
    else:
        print("    map   network-map.json  no drawing in source-of-truth/map/ — placeholder panel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
