"""Offline tests for scheduled assurance (automation/service/scheduler.py).

The scheduler opens sessions to every device, so what matters is: it is off
unless a deployment asks for it, one device failing never stops a run, and
the exposed metrics stay bounded (device x rule) with rule detail kept out
of labels.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

RULES = [
    {"rule": "admin_up_interfaces_are_operational", "severity": "error", "source": "interfaces",
     "status": "pass", "failures": []},
    {"rule": "bgp_peers_established", "severity": "error", "source": "pyats",
     "status": "fail", "failures": [{"peer": "10.0.0.1", "detail": "Idle"}]},
]


@pytest.fixture
def load(monkeypatch):
    """Import the scheduler against a fake tasks module (no devices touched)."""
    def _load(interval="15", devices=("r1", "r2"), fail=()):
        monkeypatch.setenv("ASSURANCE_INTERVAL_MINUTES", interval)
        tasks = types.ModuleType("automation.nornir.tasks")
        tasks.list_devices = lambda: [{"name": d} for d in devices]

        def run_assurance(device):
            if device in fail:
                raise RuntimeError("SSH timed out")
            return {"results": RULES}

        tasks.run_assurance = run_assurance
        import automation.nornir as nornir_pkg
        monkeypatch.setattr(nornir_pkg, "tasks", tasks, raising=False)
        monkeypatch.setitem(sys.modules, "automation.nornir.tasks", tasks)
        sys.modules.pop("automation.service.scheduler", None)
        return importlib.import_module("automation.service.scheduler")
    yield _load
    sys.modules.pop("automation.service.scheduler", None)


def test_off_by_default(load, monkeypatch):
    mod = load(interval="0")
    assert mod.start() is False
    assert "assurance_interval_seconds 0" in mod.metrics()


def test_compose_and_env_example_ship_it_off():
    env = yaml.safe_load((ROOT / "compose/automation.yaml").read_text())["services"]["automation"]["environment"]
    assert env["ASSURANCE_INTERVAL_MINUTES"] == "${ASSURANCE_INTERVAL_MINUTES:-0}"
    assert "ASSURANCE_INTERVAL_MINUTES=0" in (ROOT / ".env.example").read_text()


def test_one_series_per_device_and_rule(load):
    mod = load()
    assert mod.run_once() == 2
    text = mod.metrics()
    states = [line for line in text.splitlines() if line.startswith("assurance_rule_state{")]
    assert len(states) == 4, "2 devices x 2 rules"
    assert any('device="r1"' in s and 'rule="bgp_peers_established"' in s and 'state="fail"' in s for s in states)
    assert "assurance_interval_seconds 900" in text
    assert 'assurance_device_run_ok{device="r1"} 1' in text


def test_rule_detail_never_becomes_a_label(load):
    mod = load()
    mod.run_once()
    assert "10.0.0.1" not in mod.metrics()
    assert mod.latest()["devices"]["r1"]["rules"][1]["failures"][0]["peer"] == "10.0.0.1"


def test_one_failing_device_does_not_stop_the_run(load):
    mod = load(fail=("r1",))
    mod.run_once()
    text = mod.metrics()
    assert 'assurance_device_run_ok{device="r1"} 0' in text
    assert 'assurance_device_run_ok{device="r2"} 1' in text
    assert mod.latest()["devices"]["r1"]["error"] == "SSH timed out"


def test_device_removed_from_inventory_stops_being_reported(load):
    mod = load(devices=("r1", "r2"))
    mod.run_once()
    sys.modules["automation.nornir.tasks"].list_devices = lambda: [{"name": "r2"}]
    mod.run_once()
    assert 'device="r1"' not in mod.metrics()


def test_label_values_are_escaped(load):
    mod = load(devices=('odd"name',))
    mod.run_once()
    assert 'device="odd\\"name"' in mod.metrics()


def test_prometheus_scrapes_the_automation_service():
    prom = yaml.safe_load((ROOT / "observability/prometheus/prometheus.yml").read_text())
    jobs = {j["job_name"]: j for j in prom["scrape_configs"]}
    assert "automation:8100" in jobs["automation"]["static_configs"][0]["targets"]


def test_alerts_and_rules_cover_assurance():
    rules = (ROOT / "observability/prometheus/rules/recording.yml").read_text()
    alerts = (ROOT / "observability/prometheus/rules/alerts.yml").read_text()
    for name in ("network:assurance_failing:count", "network:assurance_passing:count"):
        assert name in rules
    for name in ("AssuranceCheckFailing", "AssuranceStale"):
        assert f"alert: {name}" in alerts
