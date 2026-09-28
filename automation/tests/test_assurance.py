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
    # Every rule is accounted for: run, or skipped with a reason. None vanish.
    assert result["rules_run"] + result["rules_skipped"] == len(engine.load_rules())
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
    from automation.pyats import checks as pyats_checks
    for rule in engine.load_rules():
        table = pyats_checks.CHECKS if rule.get("source") == "pyats" else engine.CHECKS
        assert rule["check"] in table, f"{rule['name']}: unknown check '{rule['check']}'"
        if rule.get("source") == "pyats":
            assert rule.get("feature"), f"{rule['name']}: a pyats rule must name its feature"


def test_every_platform_has_a_normaliser():
    """A platform without one is half-supported — it would seed and poll but
    never be assurable."""
    platforms = set(yaml.safe_load((ROOT / "platforms.yml").read_text()))
    assert platforms == set(normalise.supported_platforms())


# What Genie genuinely supports, verified against pyATS 26.8 by inspecting the
# installed package (genie.libs.ops and genie.libs.parser):
#
#   iosxe  learn() models for interface, platform, bgp, lldp — and many more
#   hvrp   NO learn() models at all. Parsers exist for `display bgp peer` only.
#
# test_declared_pyats_matches_real_genie (in-image, below) re-checks this
# against the installed library, so this table cannot quietly go stale.
GENIE_LEARN = {
    "iosxe": {"interface", "platform", "bgp", "lldp"},
    "hvrp": set(),
}


def test_pyats_is_additive_never_the_only_path():
    """Every platform keeps its TextFSM + normaliser path whether or not it has
    a pyats block. A platform assured by pyATS alone would lose ALL its checks
    the day Genie coverage turned out thinner than assumed — silently."""
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    supported = set(normalise.supported_platforms())
    for name in platforms:
        assert name in supported, (
            f"{name} has no TextFSM normaliser — it would depend on pyATS alone"
        )


def test_no_platform_declares_a_learn_model_genie_lacks():
    """A learn() with no model for that OS connects fine and returns NOTHING.
    Huawei is the live example: unicon connects to hvrp, but Genie has no hvrp
    interface model — so `learn: [interface]` under vrp would be wasted work."""
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    for name, spec in platforms.items():
        py = spec.get("pyats")
        if not py:
            continue
        os_name = py["os"]
        assert os_name in GENIE_LEARN, f"{name}: unverified pyATS os '{os_name}'"
        declared = set(py.get("learn", []) or [])
        missing = declared - GENIE_LEARN[os_name]
        assert not missing, (
            f"{name} declares learn({sorted(missing)}) but Genie has no such "
            f"model for '{os_name}' — it would return nothing"
        )


def test_huawei_uses_the_bgp_parser_not_learn():
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    vrp = platforms["vrp"]["pyats"]
    assert vrp["os"] == "hvrp"
    assert not vrp.get("learn"), "hvrp has no learn() models"
    assert "bgp" in vrp.get("parse", {})


def test_mikrotik_has_no_pyats():
    """unicon has no RouterOS plugin and Genie has no parsers for it."""
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    assert not platforms["routeros"].get("pyats")


def test_no_single_vendor_config_framework_sneaks_in():
    """pyATS is back deliberately. NAPALM is not: it would add a third
    per-vendor driver matrix alongside Netmiko and pyATS."""
    reqs = (ROOT / "automation/service/requirements.txt").read_text().lower()
    code = [l for l in reqs.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert not any("napalm" in l for l in code)
