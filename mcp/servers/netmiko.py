"""mcp-netmiko — what the device itself says, and (optionally) changing it.

This server does NOT open SSH sessions. It calls the automation API, which owns
the credentials, the TextFSM parsing and the error handling. One code path to
devices means a capability added there is available here for free, and a fix
never has to be made twice.
"""
from __future__ import annotations

import os

from common import Backend, build, identifier

# 300s: a config push is four sequential device commands — fetch the running
# config, snapshot state, push, snapshot again — and fetching a large config
# from a busy device is slow on its own. A client timeout shorter than the work
# leaves the change applied and the caller told it failed, which is the worst
# possible outcome for a write.
api = Backend(os.environ.get("AUTOMATION_URL", "http://automation:8100"), timeout=300.0)
mcp = build("netmiko")

ALLOW_WRITE = os.environ.get("MCP_ALLOW_WRITE", "false").lower() == "true"


@mcp.tool()
def get_device_config(device: str) -> dict:
    """Fetch the running configuration of a device.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/config")
    return {
        "device": device,
        "platform": result["platform"],
        "lines": result["lines"],
        "config": result["config"],
    }


@mcp.tool()
def get_device_state(device: str) -> dict:
    """Get parsed operational state from a device — interfaces and their status.

    Returns structured rows parsed from the device's CLI output, not raw text.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    result = api.get(f"/device/{device}/state")
    return {
        "device": device,
        "platform": result["platform"],
        "command": result["command"],
        "rows": result.get("rows", []),
    }


# --- the one write tool ----------------------------------------------------
# Registered ONLY when MCP_ALLOW_WRITE=true. Not "registered but refuses":
# absent from tools/list entirely, so a caller that does not know about it
# cannot discover it. Turning an AI loose on device configuration should be a
# deliberate act, not a side effect of a container starting.
if ALLOW_WRITE:

    @mcp.tool()
    def push_device_config(device: str, lines: list[str]) -> dict:
        """Push configuration lines to a device. THIS CHANGES THE NETWORK.

        The device's running configuration is archived before anything is sent.

        Args:
            device: the device name as it appears in the source of truth.
            lines: configuration lines to apply, in order.
        """
        identifier(device, "device")
        if not lines:
            raise ValueError("no configuration lines supplied")
        result = api.post(f"/device/{device}/config", json={"lines": lines})
        return {
            "device": device,
            "lines_sent": result["lines_sent"],
            "previous_config_archived_to": result["config_archived_to"],
            "output": result["output"],
        }
