"""The pyATS endpoints of the automation API — the real app, inside the image.

mcp-pyats fronts these, so they are what an ai-platform's "is BGP healthy?"
ultimately runs. Pinned here:

- features come only from platforms.yml (iosxe learn models, VRP bgp parse,
  nothing for RouterOS) and a platform without Genie says so
- a feature outside the allow-list is refused with the choices, and never
  opens a session; malformed names are refused before any lookup
- ok / absent / error stay distinct — absent is an answer, never ok
- the device lock is held while pyATS collects (unicon opens its own SSH
  session, which must not overlap Netmiko's)
- /bgp is compact, the same shape on every OS, and keeps one peer address
  in several VRFs as separate sessions

Needs darqcube/automation:local. No stack, no devices: pyats_api_check.py
mounts the repo's current automation/ tree and replaces only the inventory
and the pyATS session, using Genie's own golden fixtures.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "darqcube/automation:local"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")

EXPECTED = [
    "features_iosxe_from_platforms_yml", "features_vrp_is_bgp_parse_only",
    "features_platform_without_genie_says_so", "features_unknown_device_404",
    "learn_ok_returns_genie_data", "learn_collects_only_the_feature_asked_for",
    "learn_holds_the_device_lock", "learn_vrp_reports_parse",
    "learn_feature_outside_allowlist_400_and_lists_choices",
    "learn_outside_allowlist_never_opens_a_session", "learn_vrp_interface_refused",
    "learn_malformed_feature_400", "learn_malformed_device_400",
    "learn_absent_is_an_answer_not_ok",
    "bgp_iosxe_sessions_compact", "bgp_keeps_same_peer_in_different_vrfs",
    "bgp_vrp_same_shape", "bgp_on_platform_without_genie_400", "bgp_absent_has_no_sessions_key",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build automation")
    out = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{ROOT / 'automation'}:/app/automation:ro",
         "-v", f"{ROOT / 'platforms.yml'}:/app/platforms.yml:ro",
         IMAGE, "python", "/app/automation/tests/pyats_api_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("name", EXPECTED)
def test_pyats_api(results, name):
    assert name in results, f"{name} did not run: {sorted(results)}"
    passed, detail = results[name]
    assert passed, f"{name}: {detail}"


def test_every_check_is_pinned(results):
    """A check added to pyats_api_check.py must be listed here, or a failure
    in it would never be reported."""
    assert sorted(results) == sorted(EXPECTED)
