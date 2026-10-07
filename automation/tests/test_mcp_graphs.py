"""Graphs in chat — mcp-grafana's render_interface_graph and its image route.

Pinned: the tool renders ONE fixed panel of one fixed dashboard from validated
arguments (no dashboard, panel or query from the caller); it returns a
Markdown image on the published base URL with a short unguessable id, plus the
numbers as text; the image route serves without a bearer token (an <img>
cannot send one) while /mcp still requires one; images expire and the store is
bounded by count and bytes.

Needs darqcube/mcp:local. No stack: mcp_graph_check.py replaces Grafana's
render endpoint and Prometheus.
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
    "render_tool_registered_when_enabled", "returns_markdown_image_on_the_public_base",
    "image_id_is_short_and_unguessable", "renders_only_the_fixed_dashboard_panel",
    "render_request_carries_validated_vars_and_window", "render_expects_png_and_caps_size",
    "summary_has_the_numbers_in_readable_units", "summary_aggregates_across_series",
    "dashboard_link_scopes_device_and_interface",
    "errors_kind_uses_the_errors_panel",
    "bad_interface_refused", "bad_device_refused", "window_over_24h_refused", "unknown_kind_refused",
    "image_served_without_a_bearer_token", "image_response_is_locked_down",
    "unknown_image_404", "malformed_image_name_404", "exemption_is_only_the_image_route",
    "store_returns_what_it_kept", "store_forgets_after_ttl",
    "store_evicts_oldest_by_count", "store_evicts_oldest_by_bytes",
]


@pytest.fixture(scope="module")
def results() -> dict:
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{TESTS}:/t:ro", IMAGE, "python", "/t/mcp_graph_check.py"],
        capture_output=True, text=True, timeout=300,
    )
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert lines, f"the check produced no result:\n{out.stdout[-1500:]}\n{out.stderr[-1500:]}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("name", EXPECTED)
def test_graphs(results, name):
    assert name in results, f"{name} did not run: {sorted(results)}"
    passed, detail = results[name]
    assert passed, f"{name}: {detail}"


def test_every_check_is_pinned(results):
    assert sorted(results) == sorted(EXPECTED)
