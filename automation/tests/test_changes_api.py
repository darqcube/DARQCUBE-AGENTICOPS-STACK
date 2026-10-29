"""The automation API's /changes endpoints — the real app, inside the image.

Stage, approve and commit with the real store and rules; the raw one-step push
refused in approved mode; refusals as 403s with their reason; a failed push
recorded and never retried; every action in the audit. Only the inventory
lookup and the device push are replaced.

Needs darqcube/automation:local (docker compose build automation). No stack,
no devices: changes_api_check.py mounts this repo's automation/ tree.
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
    "raw_push_is_refused_in_approved_mode", "stage_approve_commit_pushes_exactly_the_staged_lines",
    "commit_before_approval_is_403_and_pushes_nothing", "an_approval_pushes_once",
    "an_approval_without_a_fresh_sign_in_is_403", "a_protected_line_is_refused_at_staging",
    "an_unknown_device_is_404", "a_failed_push_is_recorded_and_not_retried",
    "every_action_allowed_or_refused_is_an_event", "changes_list_by_state",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build automation")
    out = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{ROOT / 'automation'}:/app/automation:ro",
         "-v", f"{ROOT / 'platforms.yml'}:/app/platforms.yml:ro",
         IMAGE, "python", "/app/automation/tests/changes_api_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


def test_every_check_ran(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_changes_api(results, name):
    ok, detail = results[name]
    assert ok, detail
