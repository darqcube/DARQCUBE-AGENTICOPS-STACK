"""How many times do we build the inventory, and log in to a device?

Both were wrong before real hardware ever saw this stack: a single config push
built Nornir four times — four full Infrahub inventory fetches (a 400-node
GraphQL query each at the documented ceiling) and four SSH logins in quick
succession to the same device. Real AAA and `login block-for` treat that as an
attack.

Offline: Nornir and Netmiko are stubbed, so this counts calls without a device.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Nornir and Netmiko live in the automation image, not the dev venv. Stubbing
# them here is the same pattern test_render.py uses for the Infrahub SDK, and
# it is what lets this logic be tested in milliseconds with no device.
_nornir = types.ModuleType("nornir")
_nornir.InitNornir = lambda **kw: None
sys.modules.setdefault("nornir", _nornir)

_tasks_mod = types.ModuleType("nornir_netmiko.tasks")
def netmiko_send_command(*a, **kw): ...
def netmiko_send_config(*a, **kw): ...
_tasks_mod.netmiko_send_command = netmiko_send_command
_tasks_mod.netmiko_send_config = netmiko_send_config
_nm = types.ModuleType("nornir_netmiko")
_nm.tasks = _tasks_mod
sys.modules.setdefault("nornir_netmiko", _nm)
sys.modules.setdefault("nornir_netmiko.tasks", _tasks_mod)

from automation.nornir import tasks  # noqa: E402


class FakeHost:
    def __init__(self, name):
        self.name = name
        self.hostname = "10.0.0.1"
        self.platform = "ios_xe"
        self.username = self.password = ""
        self.data = {"infrahub_platform": "ios_xe", "InfrahubNode": None}


class FakeResult:
    """Nornir returns a dict of host -> MultiResult."""

    def __init__(self, value):
        self._value = value

    def __getitem__(self, _host):
        r = MagicMock()
        r.failed = False
        r.__getitem__ = lambda _s, _i: MagicMock(result=self._value)
        return r


class FakeNornir:
    """Counts .filter() views and tracks that they share one instance."""

    def __init__(self, hosts):
        self.inventory = MagicMock()
        self.inventory.hosts = hosts
        self.closed = 0
        self.runs = []

    def filter(self, **kw):
        view = FakeNornir({k: v for k, v in self.inventory.hosts.items() if k == kw.get("name")})
        view._parent = self
        return view

    def run(self, task=None, **kw):
        root = getattr(self, "_parent", self)
        root.runs.append(getattr(task, "__name__", str(task)))
        cmd = kw.get("command_string", "")
        return FakeResult("Interface  IP-Address  OK? Method Status  Protocol\n"
                          "Gi1        10.0.0.1    YES NVRAM  up      up" if cmd else "ok")

    def close_connections(self):
        root = getattr(self, "_parent", self)
        root.closed += 1


@pytest.fixture
def stub(monkeypatch, tmp_path):
    built = {"count": 0, "instance": None}

    def fake_build():
        built["count"] += 1
        nr = FakeNornir({"router1": FakeHost("router1")})
        built["instance"] = nr
        return nr

    monkeypatch.setattr(tasks, "_build_nornir", fake_build)
    monkeypatch.setattr(tasks, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tasks, "platforms", lambda: {
        "ios_xe": {"netmiko_type": "cisco_xe", "textfsm_platform": "cisco_ios",
                   "show_run": "show running-config",
                   "state_cmd": "show ip interface brief"}})
    tasks.invalidate_inventory()
    return built


def test_config_push_builds_the_inventory_once(stub):
    """Was four. Each build is a full Infrahub fetch."""
    tasks.put_config("router1", ["interface Lo99", "description test"])
    assert stub["count"] == 1, (
        f"built the inventory {stub['count']} times for one config push — "
        f"each one is a full GraphQL fetch of every device"
    )


def test_config_push_uses_one_session_and_closes_it(stub):
    """All four device operations must share one SSH connection."""
    tasks.put_config("router1", ["interface Lo99"])
    nr = stub["instance"]
    # get_config, pre-snapshot, the push itself, post-snapshot
    assert len(nr.runs) == 4, f"expected 4 operations, saw {nr.runs}"
    assert nr.closed == 1, "the session was not closed — idle sessions accumulate"


def test_inventory_is_cached_across_calls(stub):
    tasks.get_nornir("router1")
    tasks.get_nornir("router1")
    tasks.get_nornir()
    assert stub["count"] == 1, "the inventory cache is not being used"


def test_fresh_bypasses_the_cache(stub):
    """Needed straight after a seed, or a new device is invisible for the TTL."""
    tasks.get_nornir("router1")
    tasks.get_nornir("router1", fresh=True)
    assert stub["count"] == 2


def test_invalidate_forces_a_rebuild(stub):
    tasks.get_nornir("router1")
    tasks.invalidate_inventory()
    tasks.get_nornir("router1")
    assert stub["count"] == 2


def test_unknown_device_says_what_to_do(stub):
    with pytest.raises(tasks.DeviceError, match="make seed"):
        tasks.get_nornir("does-not-exist")


def test_oversized_push_is_refused_before_connecting(stub):
    """The cap must be enforced before a device is touched."""
    with pytest.raises(tasks.DeviceError, match="exceeds MAX_CONFIG_LINES"):
        tasks.put_config("router1", [f"line {i}" for i in range(500)])
    assert stub["count"] == 0, "connected to the device before rejecting the request"


def test_empty_push_is_refused(stub):
    with pytest.raises(tasks.DeviceError, match="no configuration lines"):
        tasks.put_config("router1", [])


# --- concurrency ----------------------------------------------------------
# Caching the inventory means every caller gets a view over the SAME Host
# object, and Nornir keeps the open SSH connection there. Without a per-device
# lock two requests interleave on one session and the CLI output comes back
# shredded — a failure that only appears under load, on real hardware.

def test_concurrent_reads_on_one_device_are_serialised(stub, monkeypatch):
    import threading

    overlapping = []
    active = {"n": 0}
    guard = threading.Lock()
    original = tasks._get_state

    def watched(device, nr, command, raw, own):
        with guard:
            active["n"] += 1
            if active["n"] > 1:
                overlapping.append(active["n"])
        try:
            import time as _t
            _t.sleep(0.02)          # widen the window a real command would occupy
            return original(device, nr, command, raw, own)
        finally:
            with guard:
                active["n"] -= 1

    monkeypatch.setattr(tasks, "_get_state", watched)

    threads = [threading.Thread(target=tasks.get_state, args=("router1",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlapping, (
        f"{len(overlapping)} overlapping sessions on one device — concurrent "
        f"commands would interleave on the same SSH connection"
    )


def test_different_devices_still_run_in_parallel(stub, monkeypatch):
    """The lock is per device, not global: a fleet-wide run must stay concurrent."""
    import threading
    import time as _t

    def two_hosts():
        nr = FakeNornir({"router1": FakeHost("router1"), "router2": FakeHost("router2")})
        stub["count"] += 1
        stub["instance"] = nr
        return nr

    monkeypatch.setattr(tasks, "_build_nornir", two_hosts)
    tasks.invalidate_inventory()

    peak = {"n": 0, "cur": 0}
    guard = threading.Lock()
    original = tasks._get_state

    def watched(device, nr, command, raw, own):
        with guard:
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
        try:
            _t.sleep(0.05)
            return original(device, nr, command, raw, own)
        finally:
            with guard:
                peak["cur"] -= 1

    monkeypatch.setattr(tasks, "_get_state", watched)

    threads = [threading.Thread(target=tasks.get_state, args=(d,)) for d in ("router1", "router2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak["n"] == 2, "two different devices were serialised — the lock is too coarse"


def test_a_device_lock_is_reused_not_recreated():
    """A fresh lock per call would lock nothing at all."""
    assert tasks.device_lock("router1") is tasks.device_lock("router1")
    assert tasks.device_lock("router1") is not tasks.device_lock("router2")


# --- where automation connects ----------------------------------------------

class _Val:
    def __init__(self, value):
        self.value = value


class _Node:
    def __init__(self, ip=None, host=None):
        self.management_ip = _Val(ip)
        self.management_host = _Val(host)


@pytest.mark.parametrize("node, expected", [
    (_Node(ip="10.0.0.11/24"), "10.0.0.11"),
    (_Node(ip="10.0.0.11", host="router1.lab.example"), "router1.lab.example"),
    (_Node(host="router1.lab.example"), "router1.lab.example"),
    (_Node(), ""),
])
def test_management_address_matches_the_renderer(node, expected):
    """SSH goes where the collectors poll: the DNS name when set, else the IP
    without its prefix — and an empty management_ip must not raise."""
    assert tasks.management_address(node) == expected


def test_inventory_mappings_are_valid_for_the_schema():
    """nornir-infrahub 1.2 rejects a dotted mapping whose first part is not a
    relationship, and then NO host loads: "platform.value" broke every device
    operation. The unit tests replace _build_nornir, so this checks the real
    mappings against darqcube.yml instead."""
    import re as _re
    import yaml as _yaml

    src = (Path(__file__).resolve().parents[2] / "automation/nornir/tasks.py").read_text()
    mappings = _re.findall(r'"mapping":\s*"([^"]+)"', src)
    assert mappings, "no schema_mappings found in tasks.py"
    schema = _yaml.safe_load((Path(__file__).resolve().parents[2] / "source-of-truth/schema/darqcube.yml").read_text())
    device = next(n for n in schema["nodes"] if n["name"] == "Device")
    attrs = {a["name"] for a in device["attributes"]}
    rels = {r["name"] for r in device.get("relationships", [])}
    for m in mappings:
        parts = m.split(".")
        if len(parts) == 1:
            assert m in attrs | rels, f"{m!r} is not a Device attribute or relationship"
        else:
            assert len(parts) == 2 and parts[0] in rels, f"{m!r}: a dotted mapping must start with a relationship"
