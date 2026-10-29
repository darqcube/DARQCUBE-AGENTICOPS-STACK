"""mcp-netmiko in approved mode — the real app, inside the built image.

Who gets through which door, and what identity reaches the change rules:
an agent's token stages and commits but never approves; only the approvals
audience opens /changes, and it opens no MCP tool; refusals reach the model
with their reason; a person without the approve right sees only their own
changes. The rules themselves: test_changes.py (offline).

Needs darqcube/mcp:local built from this tree (docker compose build mcp-infrahub).
No running stack; changes_mcp_check.py does the work inside the image.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
IMAGE = "darqcube/mcp:local"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")

EXPECTED = [
    "tools_are_stage_commit_and_never_approve", "stage_passes_the_verified_person_and_agent",
    "a_refusal_reaches_the_model_with_its_reason", "a_malformed_change_id_never_reaches_the_api",
    "mcp_refuses_an_approvals_token", "approvals_route_refuses_an_agents_token",
    "approvals_route_refuses_the_shared_token", "approve_passes_the_person_and_their_sign_in_time",
    "an_approval_refusal_is_a_403_with_its_reason", "an_approver_sees_every_change",
    "a_person_without_the_approve_right_sees_only_their_changes", "the_audit_is_for_approvers",
    "someone_elses_change_is_not_found", "a_malformed_change_id_is_400", "the_audit_line_names_the_agent",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{TESTS}:/t:ro", IMAGE, "python", "/t/changes_mcp_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [line for line in out.stdout.splitlines() if line.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


def test_every_check_ran(results):
    assert sorted(results) == sorted(EXPECTED)


@pytest.mark.parametrize("name", EXPECTED)
def test_approved_changes_gate(results, name):
    ok, detail = results[name]
    assert ok, detail
