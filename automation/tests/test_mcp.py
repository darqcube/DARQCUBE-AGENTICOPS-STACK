"""MCP server tests — offline, against the built image.

These check the two properties that make the MCP layer safe to point an AI at:
no tool takes a raw query, and the one tool that changes device configuration
is absent unless explicitly enabled.

Needs darqcube/mcp:local (docker compose build mcp-infrahub). No running stack.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVERS = ROOT / "mcp/servers"
IMAGE = "darqcube/mcp:local"

ALL = ["infrahub", "prometheus", "loki", "grafana", "netmiko", "assurance"]

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")


def _image_exists() -> bool:
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True).returncode == 0


def start(server: str, env: dict[str, str] | None = None) -> str:
    """Run one server briefly and return its startup line."""
    if not _image_exists():
        pytest.skip(f"{IMAGE} not built — run: docker compose build mcp-infrahub")
    cmd = ["docker", "run", "--rm", "-e", f"MCP_SERVER={server}", "-e", "PORT=9000",
           "-e", "MCP_AUTH_TOKEN=test", "-e", "GRAFANA_PASSWORD=x", "-e", "INFRAHUB_API_TOKEN=x"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [IMAGE, "timeout", "5", "python", "serve.py"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out.stdout + out.stderr


@pytest.mark.parametrize("server", ALL)
def test_server_starts_and_registers_tools(server):
    line = start(server)
    match = re.search(rf"mcp-{server} listening on :9000 with (\d+) tool", line)
    assert match, f"{server} did not start cleanly:\n{line[-800:]}"
    assert int(match.group(1)) > 0, f"{server} registered no tools"


def test_write_tool_is_absent_by_default():
    """push_device_config must not be REGISTERED when writes are off — not
    merely refuse when called. A caller that cannot see it cannot invoke it."""
    default = start("netmiko")
    explicit_false = start("netmiko", {"MCP_ALLOW_WRITE": "false"})
    count = lambda s: int(re.search(r"with (\d+) tool", s).group(1))
    assert count(default) == 2
    assert count(explicit_false) == 2


def test_write_tool_appears_only_when_explicitly_enabled():
    enabled = start("netmiko", {"MCP_ALLOW_WRITE": "true"})
    assert int(re.search(r"with (\d+) tool", enabled).group(1)) == 3


# --- static analysis of the tool surface ----------------------------------
# Cheap, and catches the mistake that matters most: a tool that takes a query
# string makes every other bound in the stack decorative.

FORBIDDEN_PARAMS = re.compile(
    r"def \w+\([^)]*\b(query|promql|logql|graphql|expr|command_string|cmd|sql)\s*:", re.S
)


@pytest.mark.parametrize("server", ALL)
def test_no_tool_accepts_a_raw_query(server):
    source = (SERVERS / f"{server}.py").read_text()
    # Only inspect functions decorated as tools.
    for block in source.split("@mcp.tool()")[1:]:
        signature = block[: block.index(":\n") + 2] if ":\n" in block else block[:400]
        assert not FORBIDDEN_PARAMS.search(signature), (
            f"{server}: a tool takes a raw query parameter —\n{signature.strip()[:200]}"
        )


@pytest.mark.parametrize("server", ALL)
def test_every_tool_has_a_docstring(server):
    """A tool with no docstring is unusable by a model: the description is the
    only thing it has to decide whether to call it."""
    source = (SERVERS / f"{server}.py").read_text()
    for block in source.split("@mcp.tool()")[1:]:
        head = block[:600]
        assert '"""' in head, f"{server}: a tool has no docstring:\n{head[:200]}"


def test_device_arguments_are_validated():
    """Device names reach a CLI command or a query selector, and they arrive
    from an AI platform rather than a person."""
    for server in ("netmiko", "assurance", "prometheus", "loki", "infrahub"):
        source = (SERVERS / f"{server}.py").read_text()
        assert "identifier(" in source, f"{server}: no argument validation"
