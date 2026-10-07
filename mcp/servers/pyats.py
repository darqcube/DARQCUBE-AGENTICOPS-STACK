"""mcp-pyats — what Genie's structured models say about a device.

Netmiko + TextFSM is how every platform is read; pyATS/Genie is ADDITIVE, and
only where Genie genuinely has a model for the OS (platforms.yml `pyats:`):

  Cisco IOS-XE   learn(): interface, platform, bgp, lldp
  Huawei VRP     parse(): bgp only — Genie has no hvrp learn() models
  MikroTik       nothing — use mcp-netmiko's get_device_state

Like mcp-netmiko and mcp-assurance, this fronts the automation API: one code
path to devices, one place for credentials, and the device lock that keeps
pyATS's own SSH session from overlapping Netmiko's.

No tool takes a Genie command or a parser name. A feature is a name from the
platform's allow-list, which list_pyats_features returns.
"""
from __future__ import annotations

import os

from common import Backend, build, identifier

# 300s, as for netmiko: learn() opens a session and runs several commands.
api = Backend(os.environ.get("AUTOMATION_URL", "http://automation:8100"), timeout=300.0)
mcp = build("pyats")


@mcp.tool()
def list_pyats_features(device: str) -> dict:
    """Which Genie features can be read from this device, and how.

    Call this first. `features` maps each name to "learn" (a full Genie ops
    model) or "parse" (one parsed command). `supported: false` means the
    platform has no Genie support — use the netmiko tools instead.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    return api.get(f"/device/{device}/pyats/features")


@mcp.tool()
def learn_device_feature(device: str, feature: str) -> dict:
    """Genie's structured model of one feature on a device.

    `status` is one of:
      ok       `data` holds Genie's structured output
      absent   the feature is not configured on this device — a real answer
      error    configured, but Genie returned nothing or the session failed

    Output can be large on a busy device; prefer get_bgp_neighbors for BGP.

    Args:
        device: the device name as it appears in the source of truth.
        feature: a name from list_pyats_features, e.g. "interface", "lldp".
    """
    identifier(device, "device")
    identifier(feature, "feature")
    return api.get(f"/device/{device}/pyats/learn/{feature}")


@mcp.tool()
def get_bgp_neighbors(device: str) -> dict:
    """Every BGP session on a device: VRF, address family, peer and state.

    Compact and the same shape on every OS Genie supports, so it suits a quick
    "is BGP healthy?" question. A session is identified by (vrf, af, peer) —
    the same peer address can have separate sessions in several VRFs.

    Args:
        device: the device name as it appears in the source of truth.
    """
    identifier(device, "device")
    return api.get(f"/device/{device}/pyats/bgp")
