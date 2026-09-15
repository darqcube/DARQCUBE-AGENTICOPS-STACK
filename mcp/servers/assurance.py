"""mcp-assurance — does the device match what we expect, and what changed.

These tools work identically for Cisco, Huawei and MikroTik because the
automation layer normalises parsed output to one shape before the rules see it.

Like mcp-netmiko, this fronts the automation API rather than connecting to
devices itself — one code path to devices, one place for credentials.
"""
from __future__ import annotations

import os

from common import Backend, build, identifier

api = Backend(os.environ.get("AUTOMATION_URL", "http://automation:8100"), timeout=300.0)
mcp = build("assurance")


@mcp.tool()
def run_device_checks(device: str) -> dict:
    """Run the assurance rules against a device and report pass or fail.

    Works for every supported platform. Rules are declared in
    automation/assurance/rules.yml and cover things like "an interface the
    operator enabled should be operationally up".

    A check that collects no data is reported as a FAILURE, not a pass.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    return api.post(f"/device/{device}/check")


@mcp.tool()
def get_device_facts(device: str) -> dict:
    """What a device should be, plus what it currently reports.

    Combines the source of truth with live state — usually the first thing
    worth knowing about a device.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    devices = api.get("/devices")["devices"]
    intended = next((d for d in devices if d["name"] == device), None)
    if not intended:
        return {"device": device, "found": False, "error": "not in the source of truth"}

    facts = {"device": device, "found": True, "intended": intended}
    try:
        state = api.get(f"/device/{device}/state")
        facts["live"] = {"command": state["command"], "rows": state.get("rows", [])}
        facts["reachable"] = True
    except RuntimeError as exc:
        # Reported, not raised: "the device is unreachable" is an answer.
        facts["reachable"] = False
        facts["error"] = str(exc)[:200]
    return facts


@mcp.tool()
def get_device_snapshot(device: str) -> dict:
    """A comparable point-in-time view of a device's interface state.

    Take one before a change and one after, and compare them to see exactly
    what moved. Counters and timers are deliberately excluded so the comparison
    is not drowned in noise.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    return api.get(f"/device/{device}/snapshot")


@mcp.tool()
def get_structured_config(device: str) -> dict:
    """The device's running configuration, parsed into structure.

    Returns interface stanzas with their descriptions and addresses rather than
    a wall of configuration text. Parsed with TTP, which handles the
    hierarchical shape of a config file.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    return api.get(f"/device/{device}/config/structured")
