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


# --- what the network serves: applications, services, hosts -----------------
# Optional in the schema; every tool returns an empty result, not an error,
# for a deployment that models only devices.

def _nodes(rel) -> list[dict]:
    """A cardinality-many relationship as a plain list of nodes."""
    return [e["node"] for e in (rel or {}).get("edges", [])]


def _node(rel) -> dict | None:
    """A cardinality-one relationship as the node itself (or None)."""
    return (rel or {}).get("node")


def _ip(value) -> str | None:
    """IPHost comes back as 10.0.0.1/32; the /32 is noise to a reader."""
    return str(value).split("/")[0] if value else None


_SITE = "site { node { name { value } site_type { value } } }"
_PREFIX = "prefix { node { name { value } prefix { value } purpose { value } gateway { node { name { value } role { value } } } } }"
_HOST_CORE = "name { value } host_type { value } address { value } operating_system { value } status { value } " + _SITE
_SERVICE_CORE = "name { value } protocol { value } port { value }"


def _service(svc: dict, with_host: bool = True, with_app: bool = True) -> dict:
    out = {"name": svc["name"], "protocol": svc["protocol"], "port": svc["port"]}
    if with_host:
        host = _node(svc.get("host")) or {}
        out["host"] = host.get("name")
        out["address"] = _ip(host.get("address"))
        out["site"] = (_node(host.get("site")) or {}).get("name")
    if with_app:
        app = _node(svc.get("application")) or {}
        out["application"] = app.get("name")
        out["criticality"] = app.get("criticality")
    return out


@mcp.tool()
def list_applications() -> dict:
    """List every application in the source of truth.

    Returns each application's name, category, criticality, owner, the hosts
    it runs on and its number of services.
    """
    data = _query(
        "{ NetworkApplication { edges { node { name { value } category { value } criticality { value } "
        "owner { value } services { edges { node { host { node { name { value } } } } } } } } } }"
    )
    apps = []
    for app in _nodes(data["NetworkApplication"]):
        services = _nodes(app.pop("services"))
        app["hosts"] = sorted({(_node(s["host"]) or {}).get("name") for s in services} - {None})
        app["service_count"] = len(services)
        apps.append(app)
    return {"count": len(apps), "applications": apps}


@mcp.tool()
def get_application(application: str) -> dict:
    """Get one application: its services (protocol, port), the hosts and
    sites that deliver them, and its criticality and owner.

    Args:
        application: the application name, exactly as in the source of truth.
    """
    identifier(application, "application")
    data = _query(
        "query ($n: String!) { NetworkApplication(name__value: $n) { edges { node { "
        "name { value } category { value } criticality { value } owner { value } description { value } "
        "services { edges { node { %s host { node { name { value } address { value } %s } } } } } "
        "} } } }" % (_SERVICE_CORE, _SITE),
        {"n": application},
    )
    nodes = _nodes(data["NetworkApplication"])
    if not nodes:
        return {"found": False, "application": application, "error": "not in the source of truth"}
    app = nodes[0]
    app["services"] = [_service(s, with_app=False) for s in _nodes(app["services"])]
    return {"found": True, **app}


@mcp.tool()
def get_host(host: str) -> dict:
    """Get one host (server, workstation, gateway): address, site, subnet and
    its gateway device, and the services it runs with their applications.

    Args:
        host: the host name, exactly as in the source of truth.
    """
    identifier(host, "host")
    data = _query(
        "query ($n: String!) { NetworkHost(name__value: $n) { edges { node { %s %s "
        "tags { edges { node { name { value } } } } "
        "services { edges { node { %s application { node { name { value } criticality { value } } } } } } "
        "} } } }" % (_HOST_CORE, _PREFIX, _SERVICE_CORE),
        {"n": host},
    )
    nodes = _nodes(data["NetworkHost"])
    if not nodes:
        return {"found": False, "host": host, "error": "not in the source of truth"}
    h = nodes[0]
    h["site"] = (_node(h.get("site")) or {}).get("name")
    h["address"] = _ip(h.get("address"))
    prefix = _node(h.get("prefix"))
    if prefix:
        prefix["gateway"] = (_node(prefix.get("gateway")) or {}).get("name")
    h["prefix"] = prefix
    h["tags"] = [t["name"] for t in _nodes(h.get("tags"))]
    h["services"] = [_service(s, with_host=False) for s in _nodes(h["services"])]
    return {"found": True, **h}


@mcp.tool()
def get_site_services(site: str) -> dict:
    """List the hosts at one site and the application services they run.

    Args:
        site: the site name, e.g. "hq".
    """
    identifier(site, "site")
    data = _query(
        "query ($s: String!) { NetworkHost(site__name__value: $s) { edges { node { "
        "name { value } host_type { value } address { value } "
        "services { edges { node { %s application { node { name { value } criticality { value } } } } } } "
        "} } } }" % _SERVICE_CORE,
        {"s": site},
    )
    hosts = []
    for h in _nodes(data["NetworkHost"]):
        h["address"] = _ip(h.get("address"))
        h["services"] = [_service(s, with_host=False) for s in _nodes(h["services"])]
        hosts.append(h)
    apps = sorted({s["application"] for h in hosts for s in h["services"] if s.get("application")})
    return {"site": site, "host_count": len(hosts), "applications": apps, "hosts": hosts}


@mcp.tool()
def get_application_dependencies(application: str) -> dict:
    """What an application depends on in the network: for each host that
    delivers it, the host's site, subnet, the gateway device routing that
    subnet, and the other network devices at that site.

    Use it to answer "what breaks if device X fails?" or "which devices
    matter for application Y?".

    Args:
        application: the application name, exactly as in the source of truth.
    """
    identifier(application, "application")
    data = _query(
        "query ($n: String!) { NetworkApplication(name__value: $n) { edges { node { "
        "name { value } criticality { value } "
        "services { edges { node { %s host { node { name { value } address { value } %s "
        "site { node { name { value } devices { edges { node { name { value } role { value } } } } } } "
        "} } } } } } } } }" % (_SERVICE_CORE, _PREFIX),
        {"n": application},
    )
    nodes = _nodes(data["NetworkApplication"])
    if not nodes:
        return {"found": False, "application": application, "error": "not in the source of truth"}
    app = nodes[0]
    hosts: dict[str, dict] = {}
    for svc in _nodes(app["services"]):
        h = _node(svc.get("host")) or {}
        entry = hosts.setdefault(h.get("name"), {
            "host": h.get("name"),
            "address": _ip(h.get("address")),
            "site": None, "prefix": None, "gateway": None, "site_devices": [], "services": [],
        })
        site = _node(h.get("site")) or {}
        entry["site"] = site.get("name")
        entry["site_devices"] = sorted(
            ({"name": d["name"], "role": d["role"]} for d in _nodes(site.get("devices"))),
            key=lambda d: d["name"])
        prefix = _node(h.get("prefix")) or {}
        entry["prefix"] = prefix.get("prefix")
        entry["gateway"] = (_node(prefix.get("gateway")) or {}).get("name")
        entry["services"].append(f"{svc['protocol']}/{svc['port']}")
    devices = sorted({d["name"] for e in hosts.values() for d in e["site_devices"]}
                     | {e["gateway"] for e in hosts.values() if e["gateway"]})
    return {"found": True, "application": app["name"], "criticality": app["criticality"],
            "hosts": list(hosts.values()), "network_devices": devices}

