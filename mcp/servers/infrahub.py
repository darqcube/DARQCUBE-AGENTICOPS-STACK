"""mcp-infrahub — what SHOULD exist on the network."""
from __future__ import annotations

import os

from common import Backend, build, flatten, identifier

URL = os.environ.get("INFRAHUB_URL", "http://infrahub-server:8000")
TOKEN = os.environ.get("INFRAHUB_API_TOKEN", "")

api = Backend(URL, headers={"X-INFRAHUB-KEY": TOKEN})
mcp = build("infrahub")

# Composed server-side. There is deliberately no query_graphql tool: arbitrary
# read over the whole graph would make every other bound in this stack pointless.
_DEVICE_FIELDS = """
  name { value }
  role { value }
  platform { value }
  management_ip { value }
  management_host { value }
  environment { value }
  status { value }
  telemetry_mode { value }
  site { node { name { value } site_type { value } region { value } } }
  tags { edges { node { name { value } } } }
"""


def _devices(edges: list) -> list[dict]:
    """Device nodes, with tags as a plain list of names rather than edges."""
    devices = []
    for edge in edges:
        node = edge["node"]
        node["tags"] = [t["node"]["name"] for t in (node.get("tags") or {}).get("edges", [])]
        devices.append(node)
    return devices


def _query(gql: str, variables: dict | None = None):
    body = api.post("/graphql", json={"query": gql, "variables": variables or {}})
    if "errors" in body:
        raise RuntimeError(f"Infrahub: {body['errors']}")
    return flatten(body["data"])


@mcp.tool()
def list_devices() -> dict:
    """List every network device in the source of truth.

    Returns each device's name, role, platform, management address, environment,
    status, site and tags.
    """
    data = _query("{ NetworkDevice { edges { node { %s } } } }" % _DEVICE_FIELDS)
    devices = _devices(data["NetworkDevice"]["edges"])
    return {"count": len(devices), "devices": devices}


@mcp.tool()
def get_device(device: str) -> dict:
    """Get the intended configuration of one device from the source of truth.

    Args:
        device: the device name, exactly as it appears in the source of truth.
    """
    identifier(device, "device")
    data = _query(
        "query ($n: String!) { NetworkDevice(name__value: $n) { edges { node { %s } } } }"
        % _DEVICE_FIELDS,
        {"n": device},
    )
    edges = data["NetworkDevice"]["edges"]
    if not edges:
        return {"found": False, "device": device, "error": "not in the source of truth"}
    return {"found": True, **_devices(edges)[0]}


@mcp.tool()
def get_site_devices(site: str) -> dict:
    """List the devices at one site.

    Args:
        site: the site name, e.g. "hq".
    """
    identifier(site, "site")
    data = _query(
        "query ($s: String!) { NetworkDevice(site__name__value: $s) { edges { node { %s } } } }"
        % _DEVICE_FIELDS,
        {"s": site},
    )
    devices = _devices(data["NetworkDevice"]["edges"])
    return {"site": site, "count": len(devices), "devices": devices}
