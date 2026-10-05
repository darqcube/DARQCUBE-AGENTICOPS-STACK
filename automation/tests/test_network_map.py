"""Offline tests for the Network Map topology (automation/service/network_map.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from automation.service import network_map  # noqa: E402


def dev(name, lat, lng):
    return {"metric": {"device": name, "lat": str(lat), "lng": str(lng)}}


def link(a, z):
    return {"metric": {"device": a, "neighbor": z, "kind": "lldp"}}


def topology(config):
    return json.loads(config["layers"][0]["mapjson"])


def test_nodes_edges_and_viewport():
    cfg = network_map.build([dev("a", 0, 0), dev("b", 4, 8), dev("c", -2, 1)], [link("a", "b")])
    t = topology(cfg)
    assert [n["name"] for n in t["nodes"]] == ["a", "b", "c"], "every device, linked or not"
    assert [e["meta"]["endpoint_identifiers"]["names"] for e in t["edges"]] == [["a", "b"]]
    assert t["edges"][0]["coordinates"][0] == [0.0, 0.0] and t["edges"][0]["coordinates"][-1] == [4.0, 8.0]
    v = cfg["viewport"]
    assert v["top"] > 4 and v["bottom"] < -2 and v["left"] < 0 and v["right"] > 8
    assert len(cfg["layers"]) == 3, "the panel reads all three layer slots"


def test_one_edge_per_pair_and_unknown_ends_skipped():
    t = topology(network_map.build([dev("a", 0, 0), dev("b", 1, 1)],
                                   [link("a", "b"), link("b", "a"), link("a", "ghost")]))
    assert len(t["edges"]) == 1


def test_device_without_position_is_left_out():
    t = topology(network_map.build([{"metric": {"device": "old"}}, dev("a", 0, 0)], []))
    assert [n["name"] for n in t["nodes"]] == ["a"]


def test_node_label_is_escaped():
    t = topology(network_map.build([dev("<b>", 0, 0)], []))
    assert "&lt;b&gt;" in t["nodes"][0]["meta"]["svg"] and "<b>" not in t["nodes"][0]["meta"]["svg"]


def test_write_only_when_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(network_map, "OUTPUT", str(tmp_path / "network-map.json"))
    monkeypatch.setattr(network_map, "current", lambda: {"layers": [{"mapjson": "{}"}]})
    assert network_map.write_once() is True
    assert network_map.write_once() is False
    assert oct((tmp_path / "network-map.json").stat().st_mode)[-3:] == "644", "Grafana reads it as another user"


def test_off_when_interval_is_zero(monkeypatch):
    monkeypatch.setattr(network_map, "INTERVAL", 0)
    monkeypatch.setattr(network_map, "_started", False)
    assert network_map.start() is False
