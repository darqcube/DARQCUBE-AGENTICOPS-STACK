"""pyATS / Genie assurance tests.

The evaluation logic is tested OFFLINE against Genie's own golden fixtures —
real device output that Genie's maintainers parsed — copied into
automation/pyats/samples/. No device, no pyATS install needed.

One test (test_declared_pyats_matches_real_genie) runs inside the automation
image and checks platforms.yml against the installed Genie, so the claims about
what Huawei does and does not support cannot go stale.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "automation/pyats/samples"
sys.path.insert(0, str(ROOT))

from automation.pyats import checks  # noqa: E402


def sample(name):
    return json.loads((SAMPLES / name).read_text())


# --- the BGP walker, against real Genie output ----------------------------

def test_iosxe_learn_bgp_sessions_are_found():
    sessions = checks.bgp_peers(sample("iosxe-learn-bgp.json"))
    assert sessions, "no sessions found in the iosxe learn('bgp') fixture"
    assert all(s["state"] == "Established" for s in sessions)


def test_same_peer_in_two_vrfs_is_two_sessions():
    """Genie's own iosxe fixture has 2.2.2.2 in BOTH VRF1 and default. Those are
    separate BGP sessions. Collapsing them by IP would let one being down hide
    behind the other being up."""
    sessions = checks.bgp_peers(sample("iosxe-learn-bgp.json"))
    vrfs_for = {}
    for s in sessions:
        vrfs_for.setdefault(s["peer"], set()).add(s["vrf"])
    assert vrfs_for["2.2.2.2"] == {"VRF1", "default"}
    assert len(sessions) == 8          # 4 peers x 2 VRFs


def test_the_nested_transport_state_is_not_a_second_session():
    """iosxe nests bgp_session_transport.connection.state inside each peer.
    Descending into it would double the count to 16."""
    sessions = checks.bgp_peers(sample("iosxe-learn-bgp.json"))
    keys = [(s["vrf"], s["af"], s["peer"]) for s in sessions]
    assert len(keys) == len(set(keys)) == 8


def test_a_down_session_is_not_hidden_by_the_same_peer_elsewhere():
    """The failure mode deduping would cause, pinned directly."""
    data = sample("iosxe-learn-bgp.json")
    data["instance"]["default"]["vrf"]["VRF1"]["neighbor"]["2.2.2.2"]["session_state"] = "Idle"
    failures = checks.check_bgp_established(data, {})
    assert [f["interface"] for f in failures] == ["VRF1 2.2.2.2"]


def test_hvrp_display_bgp_peer_is_parsed():
    """Huawei VRP: the one thing Genie parses for hvrp."""
    sessions = checks.bgp_peers(sample("hvrp-display-bgp-peer.json"))
    by_peer = {s["peer"]: s for s in sessions}
    assert by_peer["5.5.5.5"]["state"] == "Connect"
    assert by_peer["5.5.5.5"]["af"] == "ipv4"
    assert any(s["state"] == "Established" for s in sessions)


def test_non_established_peers_fail():
    failures = checks.check_bgp_established(sample("hvrp-display-bgp-peer.json"), {})
    failed = {f["interface"] for f in failures}
    assert "default/ipv4 5.5.5.5" in failed        # names the VRF and family
    assert all("not Established" in f["detail"] for f in failures)


def test_all_established_passes():
    assert checks.check_bgp_established(sample("iosxe-learn-bgp.json"), {}) == []


def test_ignored_peers_are_not_reported():
    """For a peer that is expected to be down — a planned turn-up."""
    rule = {"ignore_peers": ["5.5.5.5"]}
    failed = {f["interface"] for f in checks.check_bgp_established(
        sample("hvrp-display-bgp-peer.json"), rule)}
    assert not any(label.endswith(" 5.5.5.5") for label in failed)


def test_no_bgp_is_not_a_failure():
    """A device that runs no BGP has nothing to fail."""
    assert checks.check_bgp_established({"vrf": {}}, {}) == []


# --- the engine: pass, skip, error ----------------------------------------

@pytest.fixture
def rows():
    from automation.textfsm import parse
    manifest = yaml.safe_load((ROOT / "automation/textfsm/samples/manifest.yml").read_text())
    by = {e["platform"]: e for e in manifest["samples"]}
    def get(platform):
        e = by[platform]
        return parse.parse_output(platform, e["command"],
                                  (ROOT / "automation/textfsm/samples" / e["file"]).read_text())
    return get


def _bgp(result):
    return next(r for r in result["results"] if r["rule"] == "bgp_peers_established")


def test_cisco_runs_both_engines(rows):
    from automation.assurance import engine
    result = engine.run_rules("ios_xe", rows("ios_xe"), {"bgp": sample("iosxe-learn-bgp.json")})
    assert result["engines"] == ["textfsm", "pyats"]
    assert _bgp(result)["status"] == "pass"


def test_huawei_bgp_failure_is_reported(rows):
    from automation.assurance import engine
    result = engine.run_rules("vrp", rows("vrp"), {"bgp": sample("hvrp-display-bgp-peer.json")})
    assert _bgp(result)["status"] == "fail"
    assert result["passed"] is False


def test_mikrotik_bgp_rule_is_skipped_not_passed(rows):
    """No pyATS support must read as SKIPPED — a pass would claim a check ran,
    a fail would blame the device for a gap in Genie's coverage."""
    from automation.assurance import engine
    result = engine.run_rules("routeros", rows("routeros"))
    bgp = _bgp(result)
    assert bgp["status"] == "skipped"
    assert "no pyATS" in bgp["detail"]
    assert result["rules_skipped"] == 1


def test_a_skip_does_not_fail_the_device(rows):
    from automation.assurance import engine
    result = engine.run_rules("routeros", rows("routeros"))
    # The two interface rules decide pass/fail; the skipped BGP rule does not.
    assert result["rules_failed"] == 0 or any(
        r["status"] == "fail" and r["source"] == "interfaces" for r in result["results"])


def test_a_pyats_session_error_is_an_error_not_a_skip(rows):
    """If pyATS could not connect, the BGP rule must say so — not look like a
    coverage skip, and not pass."""
    from automation.assurance import engine
    result = engine.run_rules("ios_xe", rows("ios_xe"), {}, pyats_error="auth failed")
    bgp = _bgp(result)
    assert bgp["status"] == "error" and "auth failed" in bgp["detail"]
    assert result["passed"] is False


def test_textfsm_results_survive_a_pyats_failure(rows):
    from automation.assurance import engine
    result = engine.run_rules("ios_xe", rows("ios_xe"), {}, pyats_error="unreachable")
    interface_rules = [r for r in result["results"] if r["source"] == "interfaces"]
    assert interface_rules and all(r["status"] in ("pass", "fail") for r in interface_rules)


# --- against the installed Genie ------------------------------------------

@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_declared_pyats_matches_real_genie():
    """Checks platforms.yml against the Genie actually installed in the image:
    every declared os has a unicon plugin, every learn() feature has an ops
    model for that os, and every parse() command has a parser."""
    if subprocess.run(["docker", "image", "inspect", "darqcube/automation:local"],
                      capture_output=True).returncode != 0:
        pytest.skip("darqcube/automation:local not built")

    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    declared = {n: s["pyats"] for n, s in platforms.items() if s.get("pyats")}
    probe = f"""
import json, os, pkgutil
import unicon.plugins, genie.libs.ops as O
from genie.libs.parser.utils import get_parser
declared = json.loads({json.dumps(json.dumps(declared))})
plugins = {{n for _, n, p in pkgutil.iter_modules([os.path.dirname(unicon.plugins.__file__)]) if p}}
problems = []
for name, spec in declared.items():
    o = spec["os"]
    if o not in plugins:
        problems.append(f"{{name}}: no unicon plugin '{{o}}'")
    for feat in spec.get("learn", []) or []:
        d = os.path.join(os.path.dirname(O.__file__), feat, o)
        if not os.path.isdir(d):
            problems.append(f"{{name}}: no Genie learn('{{feat}}') model for '{{o}}'")
    for feat, cmd in (spec.get("parse", {{}}) or {{}}).items():
        class D: os = o; custom = {{}}
        try:
            get_parser(cmd, D())
        except Exception as exc:
            problems.append(f"{{name}}: no Genie parser for '{{cmd}}' on '{{o}}': {{exc}}")
print(json.dumps(problems))
"""
    out = subprocess.run(["docker", "run", "-i", "--rm", "darqcube/automation:local", "python", "-"],
                         input=probe, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-800:]
    problems = json.loads(out.stdout.strip().splitlines()[-1])
    assert not problems, "platforms.yml claims support Genie does not have:\n  " + "\n  ".join(problems)
