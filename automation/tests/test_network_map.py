"""Offline tests for the Network Map dashboard generator
(source-of-truth/scripts/render-network-map.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_network_map", ROOT / "source-of-truth/scripts/render-network-map.py")
nm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nm)

DRAWING = {
    "map": {},
    "nodes": [{"name": "r1", "x": 100, "y": 100}, {"name": "r2", "x": 300, "y": 100, "icon": "switch"},
              {"name": "host", "x": 300, "y": 300, "icon": "pc"}, {"name": "net", "x": 100, "y": -50, "icon": "cloud"}],
    "links": [{"a": "r1:Et0/1", "z": "r2:Et0/2"},
              {"a": "host:eth1", "z": "r2:Et0/3", "bandwidth": "100M"},
              {"a": "net:x", "z": "host:y"}],
}


def panel(drawing=DRAWING, polled=("r1", "r2")):
    d = nm.dashboard(drawing, set(polled))
    return d, next(p for p in d["panels"] if p["type"] == "tamirsuliman-weathermap-panel")


def link(p, i):
    return p["options"]["weathermap"]["links"][i]


def test_traffic_comes_from_the_polled_end():
    _, p = panel()
    a = link(p, 0)["sides"]   # r1 polled: A->Z is r1's out
    assert (a["A"]["query"], a["Z"]["query"]) == ("r1 Et0/1 out", "r1 Et0/1 in")
    h = link(p, 1)["sides"]   # host not polled: A(host)->Z(r2) is r2's in
    assert (h["A"]["query"], h["Z"]["query"]) == ("r2 Et0/3 in", "r2 Et0/3 out")
    n = link(p, 2)["sides"]   # neither end polled: drawn, no data
    assert "query" not in n["A"] and "query" not in n["Z"]


def test_bandwidth_from_drawing_else_ifspeed():
    _, p = panel()
    assert link(p, 0)["sides"]["A"]["bandwidthQuery"] == "r1 Et0/1 speed"
    assert link(p, 1)["sides"]["A"]["bandwidth"] == 100e6 and "bandwidthQuery" not in link(p, 1)["sides"]["A"]


def test_every_side_query_matches_a_target_legend():
    """A side shows the series whose display name equals its query — a typo
    leaves the link grey with no error anywhere."""
    _, p = panel()
    legends = [t["legendFormat"] for t in p["targets"]]
    pattern = "|".join(l.replace("{{device}}", r"\S+").replace("{{ifName}}", r"\S+") for l in legends)
    import re
    for l in p["options"]["weathermap"]["links"]:
        for side in l["sides"].values():
            for key in ("query", "bandwidthQuery"):
                if key in side:
                    assert re.fullmatch(pattern, side[key]), side[key]
    for n in p["options"]["weathermap"]["nodes"]:
        if "statusQuery" in n:
            assert re.fullmatch(pattern, n["statusQuery"])


def test_port_labels_and_status_only_for_devices():
    _, p = panel()
    assert link(p, 0)["sides"]["A"]["portLabel"] == "Et0/1" and link(p, 0)["sides"]["Z"]["portLabel"] == "Et0/2"
    status = {n["label"]: n.get("statusQuery") for n in p["options"]["weathermap"]["nodes"]}
    assert status == {"r1": "STATUS r1", "r2": "STATUS r2", "host": None, "net": None}


def test_vertical_scale_spreads_and_animation_speed():
    _, p = panel({**DRAWING, "map": {"scale": {"x": 1, "y": 2}, "animation_seconds": 0.5}})
    wm = p["options"]["weathermap"]
    pos = {n["label"]: n["position"] for n in wm["nodes"]}
    assert pos["r2"][0] - pos["r1"][0] == 200 and pos["host"][1] - pos["r2"][1] == 400
    assert wm["settings"]["link"]["flowAnimation"]["speed"] == 0.5


def test_positions_shifted_to_the_margin_and_scaled():
    _, p = panel({**DRAWING, "map": {"scale": 2}})
    pos = {n["label"]: n["position"] for n in p["options"]["weathermap"]["nodes"]}
    assert pos["net"] == [nm.MARGIN, nm.MARGIN], "top-left node sits at the margin"
    assert pos["r2"][0] - pos["r1"][0] == 400
    size = p["options"]["weathermap"]["settings"]["panel"]["panelSize"]
    assert size["width"] == 400 + 2 * nm.MARGIN and size["height"] == 700 + 2 * nm.MARGIN


@pytest.mark.parametrize("bad, message", [
    ({"links": [{"a": "r1", "z": "r2:Et0/1"}]}, "must be <node>:<interface>"),
    ({"links": [{"a": "ghost:x", "z": "r2:Et0/1"}]}, "not in nodes"),
    ({"nodes": [{"name": "r1", "x": 0, "y": 0}, {"name": "r1", "x": 1, "y": 1}], "links": []}, "unique name"),
])
def test_bad_drawings_are_refused(bad, message):
    with pytest.raises(nm.DrawingError, match=message):
        nm.dashboard({**DRAWING, **bad}, set())


def test_unknown_top_level_key_is_refused(tmp_path):
    (tmp_path / "t.yml").write_text("nodez: []\n")
    with pytest.raises(nm.DrawingError, match="unknown top-level"):
        nm.load_drawing(str(tmp_path))


def test_no_drawing_gives_a_placeholder_not_an_error(tmp_path):
    assert nm.load_drawing(str(tmp_path)) is None
    d = nm.dashboard(None, set())
    assert d["uid"] == "darqcube-network-map"
    assert any(p["type"] == "text" for p in d["panels"])
    assert not any(p["type"] == "tamirsuliman-weathermap-panel" for p in d["panels"])


def test_shipped_example_renders():
    drawing = yaml.safe_load((ROOT / "source-of-truth/map/examples/topology.yml").read_text())
    _, p = panel(drawing, polled=("core-01",))
    assert len(p["options"]["weathermap"]["links"]) == len(drawing["links"])
    json.dumps(p)


def test_wiring():
    """make render runs the generator, infrahub-server can read the drawing and
    write the dashboard, Grafana provisions it and installs the panel plugin."""
    make = (ROOT / "Makefile").read_text()
    render = make[make.index("\nrender:"):make.index("\n\n", make.index("\nrender:"))]
    assert "render-network-map.py" in render
    sot = yaml.safe_load((ROOT / "compose/source-of-truth.yaml").read_text())["services"]["infrahub-server"]["volumes"]
    assert "../source-of-truth/map:/map:ro" in sot
    assert "../observability/grafana/provisioning/dashboards/generated:/grafana-dashboards" in sot
    provider = yaml.safe_load((ROOT / "observability/grafana/provisioning/dashboards/provider.yml").read_text())
    assert "/etc/grafana/provisioning/dashboards/generated" in [p["options"]["path"] for p in provider["providers"]]
    grafana = yaml.safe_load((ROOT / "compose/observability.yaml").read_text())["services"]["grafana"]["environment"]
    assert "tamirsuliman-weathermap-panel" in grafana["GF_PLUGINS_PREINSTALL"]
    assert grafana["GF_PLUGINS_PREINSTALL_ASYNC"] == "false"
    ignore = (ROOT / ".gitignore").read_text()
    assert "source-of-truth/map/*.yml" in ignore, "a deployment's drawing is its own, never committed"
