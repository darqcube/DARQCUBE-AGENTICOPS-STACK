"""Assurance engine tests — vendor-neutral checks over normalised state.

Offline: runs against the committed CLI samples, no stack and no devices.

The engine normalises all three platforms into one shape, so a single set of
rules covers the fleet rather than one implementation per vendor.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "automation/textfsm/samples"
sys.path.insert(0, str(ROOT))

from automation.assurance import engine, normalise  # noqa: E402
from automation.textfsm import parse as textfsm_parse  # noqa: E402

MANIFEST = yaml.safe_load((SAMPLES / "manifest.yml").read_text())["samples"]


def rows_for(entry):
    return textfsm_parse.parse_output(
        entry["platform"], entry["command"], (SAMPLES / entry["file"]).read_text()
    )


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["platform"])
def test_every_platform_normalises(entry):
    """One shape out of three completely different CLI formats."""
    interfaces = normalise.normalise_interfaces(entry["platform"], rows_for(entry))
    assert interfaces
    for iface in interfaces:
        assert iface["interface"]
        assert isinstance(iface["admin_up"], bool)
        assert isinstance(iface["oper_up"], bool)


@pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["platform"])
def test_every_platform_runs_the_same_rules(entry):
    result = engine.run_rules(entry["platform"], rows_for(entry))
    assert result["rules_run"] == len(engine.load_rules())
    assert result["interfaces_checked"] > 0


def test_admin_down_interface_is_not_reported_as_a_fault():
    """A deliberately shut port is not a problem. Cisco's Gi2 is
    'administratively down'; RouterOS ether2 carries the X (disabled) flag."""
    by_platform = {e["platform"]: e for e in MANIFEST}

    cisco = normalise.normalise_interfaces("ios_xe", rows_for(by_platform["ios_xe"]))
    gi2 = next(i for i in cisco if i["interface"] == "GigabitEthernet2")
    assert gi2["admin_up"] is False

    ros = normalise.normalise_interfaces("routeros", rows_for(by_platform["routeros"]))
    eth2 = next(i for i in ros if i["interface"] == "ether2")
    assert eth2["admin_up"] is False


def test_running_interface_is_recognised_on_routeros():
    """RouterOS flags live in `status`, not `flags`. Reading the wrong field
    reported every running interface as down — this pins the fix."""
    entry = next(e for e in MANIFEST if e["platform"] == "routeros")
    interfaces = normalise.normalise_interfaces("routeros", rows_for(entry))
    ether1 = next(i for i in interfaces if i["interface"] == "ether1")
    assert ether1["oper_up"] is True and ether1["admin_up"] is True


def test_field_mismatch_raises_rather_than_guessing():
    """A normaliser reading a field the parser does not produce yields
    plausible but WRONG booleans — worse than an error, because it still looks
    like data. It must raise instead."""
    with pytest.raises(normalise.NormaliseError, match="missing"):
        normalise.normalise_interfaces("routeros", [{"name": "ether1"}])


def test_unknown_platform_raises_with_a_useful_message():
    with pytest.raises(normalise.NormaliseError, match="no interface normaliser"):
        normalise.normalise_interfaces("nx_os", [{"interface": "Eth1/1"}])


def test_comparison_detects_a_change():
    before = {"Gi1": {"admin_up": True, "oper_up": True}}
    after = {"Gi1": {"admin_up": True, "oper_up": False}}
    diff = engine.compare(before, after)
    assert diff["changed"] and diff["change_count"] == 1
    assert diff["changes"][0]["from"] is True and diff["changes"][0]["to"] is False


def test_comparison_of_identical_snapshots_reports_no_change():
    snap = {"Gi1": {"admin_up": True, "oper_up": True}}
    assert engine.compare(snap, dict(snap))["changed"] is False


def test_comparison_refuses_an_empty_snapshot():
    """An empty side diffs clean against anything, which would report a
    change-free result that means nothing."""
    with pytest.raises(ValueError, match="empty"):
        engine.compare({}, {"Gi1": {"admin_up": True, "oper_up": True}})


def test_every_rule_names_a_check_that_exists():
    for rule in engine.load_rules():
        assert rule["check"] in engine.CHECKS, f"{rule['name']}: unknown check"


def test_every_platform_has_a_normaliser():
    """A platform without one is half-supported — it would seed and poll but
    never be assurable."""
    platforms = set(yaml.safe_load((ROOT / "platforms.yml").read_text()))
    assert platforms == set(normalise.supported_platforms())


def test_assurance_stays_single_path():
    """Assurance must stay one implementation for every vendor.

    A vendor-specific assurance library would work for one platform and need a
    parallel implementation for the rest — two paths that must agree, and one
    of them always lags. This asserts nobody reintroduces that shape by
    accident; doing it on purpose means changing this test too.
    """
    # A DENYLIST, not an inventory: none of these are in the project, and this
    # test passes precisely because none of them are found. They are listed
    # because each one brings a per-vendor state model or driver matrix — the
    # shape this stack deliberately avoids.
    reqs = (ROOT / "automation/service/requirements.txt").read_text().lower()
    rejected = ["pyats", "genie", "napalm"]
    found = [f for f in rejected if f in reqs]
    assert not found, (
        f"{found} pulls in a vendor-specific state model. Assurance goes "
        f"through automation/assurance/normalise.py so one rule set covers "
        f"every platform."
    )

    # Every platform must reach the same engine, with no per-vendor bypass.
    platforms = set(yaml.safe_load((ROOT / "platforms.yml").read_text()))
    assert platforms == set(normalise.supported_platforms())
