"""Offline tests for render-inventory.py.

The Infrahub SDK is stubbed, so these run with no stack and no network — which
means the renderer's logic is testable long before Infrahub is up, and a
regression is caught in seconds rather than after a deploy.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


class Attr:
    def __init__(self, value):
        self.value = value


class Peer:
    def __init__(self, name):
        self.name = Attr(name)


class Rel:
    def __init__(self, name):
        self.peer = Peer(name)


class Device:
    """Stands in for an infrahub_sdk InfrahubNode."""

    def __init__(self, name, platform, ip, site, role, mode="snmp"):
        self.name = Attr(name)
        self.platform = Attr(platform)
        self.management_ip = Attr(ip)
        self.role = Attr(role)
        self.telemetry_mode = Attr(mode)
        self.site = Rel(site)


def load_renderer(monkeypatch, devices, out_dir):
    """Import render-inventory.py with the SDK replaced by a stub."""
    sdk = types.ModuleType("infrahub_sdk")

    class StubClient:
        def __init__(self, *a, **kw):
            pass

        def filters(self, **kw):
            return devices

    sdk.InfrahubClientSync = StubClient
    sdk.Config = lambda **kw: None
    monkeypatch.setitem(sys.modules, "infrahub_sdk", sdk)

    monkeypatch.setenv("INFRAHUB_API_TOKEN", "test")
    monkeypatch.setenv("PLATFORMS_FILE", str(ROOT / "platforms.yml"))
    monkeypatch.setenv("PROFILE_DIR", str(ROOT / "observability/telegraf/profiles"))
    monkeypatch.setenv("RENDER_OUT_DIR", str(out_dir))

    spec = importlib.util.spec_from_file_location(
        "render_inventory", ROOT / "source-of-truth/scripts/render-inventory.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


THREE = [
    Device("cr1", "ios_xe", "10.0.0.11/24", "hq", "core"),
    Device("sw-hw-01", "vrp", "10.0.0.21", "hq", "access"),
    Device("mt-01", "routeros", "10.0.0.31", "branch-01", "wan"),
]


def test_renders_one_resource_file_per_platform(monkeypatch, tmp_path):
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0

    produced = {p.name for p in tmp_path.iterdir()}
    assert produced == {
        "snmp-interfaces.conf",
        "snmp-ios_xe.conf",
        "snmp-vrp.conf",
        "snmp-routeros.conf",
        "devices.json",
        "devices.yml",
    }


def test_interfaces_input_covers_every_device(monkeypatch, tmp_path):
    """IF-MIB is shared: one input must list all three agents, not one each."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    body = (tmp_path / "snmp-interfaces.conf").read_text()
    for ip in ("10.0.0.11", "10.0.0.21", "10.0.0.31"):
        assert f'"udp://{ip}:161"' in body
    assert body.count("[[inputs.snmp]]") == 1


def test_management_ip_prefix_is_stripped(monkeypatch, tmp_path):
    """An IPHost attribute may carry /24 — polling that address would fail."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    body = (tmp_path / "snmp-interfaces.conf").read_text()
    assert '"udp://10.0.0.11:161"' in body
    assert "10.0.0.11/24" not in body


def test_identity_table_keyed_by_both_ip_and_name(monkeypatch, tmp_path):
    """Telegraf matches on agent IP, Logstash on hostname — both must resolve."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    identity = json.loads((tmp_path / "devices.json").read_text())
    assert identity["10.0.0.11"]["device"] == "cr1"
    assert identity["cr1"]["device"] == "cr1"
    assert identity["cr1"]["site"] == "hq"
    assert identity["mt-01"]["role"] == "wan"


def test_memory_kind_reaches_the_config(monkeypatch, tmp_path):
    """The unit each vendor reports must survive into the collector as a tag,
    or the Prometheus recording rule cannot tell bytes from percent."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    assert 'memory_kind = "percent"' in (tmp_path / "snmp-vrp.conf").read_text()
    assert 'memory_kind = "used_free"' in (tmp_path / "snmp-ios_xe.conf").read_text()
    assert 'memory_kind = "used_total"' in (tmp_path / "snmp-routeros.conf").read_text()


def test_gnmi_device_is_not_also_polled_by_snmp(monkeypatch, tmp_path):
    """telemetry_mode is exclusive — collecting both would double-count."""
    devices = [Device("cr1", "ios_xe", "10.0.0.11", "hq", "core", mode="gnmi")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert (tmp_path / "gnmi.conf").exists()
    assert not (tmp_path / "snmp-interfaces.conf").exists()


def test_gnmi_on_unsupported_platform_warns_and_skips(monkeypatch, tmp_path, capsys):
    """RouterOS has no gNMI. Asking for it must be loud, not silently empty."""
    devices = [Device("mt-01", "routeros", "10.0.0.31", "hq", "wan", mode="gnmi")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    err = capsys.readouterr().err
    assert "does not support gNMI" in err
    assert not (tmp_path / "gnmi.conf").exists()


def test_unknown_platform_warns_and_skips(monkeypatch, tmp_path, capsys):
    devices = [Device("x1", "ios_xr", "10.0.0.99", "hq", "core")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert "has no entry in platforms.yml" in capsys.readouterr().err


def test_schema_platforms_match_platforms_yml():
    """A platform in one place and not the other means a device can be seeded
    that no collector will ever poll — and it fails silently."""
    schema = yaml.safe_load((ROOT / "source-of-truth/schema/darqcube.yml").read_text())
    device = next(n for n in schema["nodes"] if n["name"] == "Device")
    attr = next(a for a in device["attributes"] if a["name"] == "platform")
    assert {c["name"] for c in attr["choices"]} == set(
        yaml.safe_load((ROOT / "platforms.yml").read_text())
    )


def test_no_schema_description_hits_the_128_char_limit():
    """Infrahub rejects the ENTIRE schema load with string_too_long and does
    not name the field, so this is checked here instead of by bisecting."""
    text = (ROOT / "source-of-truth/schema/darqcube.yml").read_text()
    schema = yaml.safe_load(text)
    too_long = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str) and len(value) >= 128:
                    too_long.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    assert not too_long, f"descriptions >=128 chars: {too_long}"
