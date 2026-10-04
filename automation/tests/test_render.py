"""Offline tests for render-inventory.py.

The Infrahub SDK is stubbed, so these run with no stack and no network — which
means the renderer's logic is testable long before Infrahub is up, and a
regression is caught in seconds rather than after a deploy.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


class Attr:
    def __init__(self, value):
        self.value = value


class Peer:
    def __init__(self, name):
        self.name = Attr(name)


class Rel:
    def __init__(self, name):
        self.peer = Peer(name)


class Device:
    """Stands in for an infrahub_sdk InfrahubNode."""

    def __init__(self, name, platform, ip, site, role, mode="snmp", host=None, security=None,
                 flow=False, flow_port=None):
        self.name = Attr(name)
        self.platform = Attr(platform)
        self.management_ip = Attr(ip)
        self.management_host = Attr(host)
        self.snmp_security = Attr(security)
        self.role = Attr(role)
        self.telemetry_mode = Attr(mode)
        self.flow_enabled = Attr(flow)
        self.flow_port = Attr(flow_port)
        self.site = Rel(site)


def load_renderer(monkeypatch, devices, out_dir, services=()):
    """Import render-inventory.py with the SDK replaced by a stub."""
    sdk = types.ModuleType("infrahub_sdk")

    class StubClient:
        def __init__(self, *a, **kw):
            pass

        def filters(self, **kw):
            return list(services) if kw.get("kind") == "NetworkService" else devices

    sdk.InfrahubClientSync = StubClient
    sdk.Config = lambda **kw: None
    monkeypatch.setitem(sys.modules, "infrahub_sdk", sdk)

    monkeypatch.setenv("INFRAHUB_API_TOKEN", "test")
    monkeypatch.setenv("PLATFORMS_FILE", str(ROOT / "platforms.yml"))
    monkeypatch.setenv("PROFILE_DIR", str(ROOT / "observability/telegraf/profiles"))
    monkeypatch.setenv("RENDER_OUT_DIR", str(out_dir))

    spec = importlib.util.spec_from_file_location(
        "render_inventory", ROOT / "source-of-truth/scripts/render-inventory.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


THREE = [
    Device("cr1", "ios_xe", "10.0.0.11/24", "hq", "core"),
    Device("sw-hw-01", "vrp", "10.0.0.21", "hq", "access"),
    Device("mt-01", "routeros", "10.0.0.31", "branch-01", "wan"),
]


def test_renders_one_resource_file_per_platform(monkeypatch, tmp_path):
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0

    produced = {p.name for p in tmp_path.iterdir()}
    assert produced == {
        "snmp-interfaces.conf",
        "snmp-ios_xe.conf",
        "snmp-vrp.conf",
        "snmp-routeros.conf",
        "services-dst.json",
        "services-src.json",
        "devices.json",
        "devices.yml",
    }


def test_interfaces_input_covers_every_device(monkeypatch, tmp_path):
    """IF-MIB is shared: one input must list all three agents, not one each."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    body = (tmp_path / "snmp-interfaces.conf").read_text()
    for ip in ("10.0.0.11", "10.0.0.21", "10.0.0.31"):
        assert f'"udp://{ip}:161"' in body
    assert body.count("[[inputs.snmp]]") == 1


def test_management_ip_prefix_is_stripped(monkeypatch, tmp_path):
    """An IPHost attribute may carry /24 — polling that address would fail."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    body = (tmp_path / "snmp-interfaces.conf").read_text()
    assert '"udp://10.0.0.11:161"' in body
    assert "10.0.0.11/24" not in body


def test_identity_table_keyed_by_both_ip_and_name(monkeypatch, tmp_path):
    """Telegraf matches on agent IP, Logstash on hostname — both must resolve."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    identity = json.loads((tmp_path / "devices.json").read_text())
    assert identity["10.0.0.11"]["device"] == "cr1"
    assert identity["cr1"]["device"] == "cr1"
    assert identity["cr1"]["site"] == "hq"
    assert identity["mt-01"]["role"] == "wan"


def test_management_host_is_polled_instead_of_the_ip(monkeypatch, tmp_path):
    devices = [Device("cr1", "ios_xe", "10.0.0.11", "hq", "core-wan", host="cr1.lab.example")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    body = (tmp_path / "snmp-interfaces.conf").read_text()
    assert '"udp://cr1.lab.example:161"' in body
    assert "10.0.0.11" not in body


def test_polled_by_name_still_resolves_flows_by_ip(monkeypatch, tmp_path):
    """SNMP metrics key on the agent (the DNS name); flow records key on the
    exporter's source address. Both must find the same identity."""
    devices = [Device("cr1", "ios_xe", "10.0.0.11/24", "hq", "core-wan", host="cr1.lab.example")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    identity = json.loads((tmp_path / "devices.json").read_text())
    assert identity["cr1.lab.example"]["device"] == "cr1"
    assert identity["10.0.0.11"]["device"] == "cr1"


def test_device_with_only_a_management_host(monkeypatch, tmp_path):
    devices = [Device("cr1", "ios_xe", None, "hq", "core-wan", host="cr1.lab.example")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    assert mod.main() == 0
    assert '"udp://cr1.lab.example:161"' in (tmp_path / "snmp-interfaces.conf").read_text()


def test_default_security_is_auth_priv_with_no_placeholder_left(monkeypatch, tmp_path):
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    for conf in tmp_path.glob("*.conf"):
        body = conf.read_text()
        assert "__SECURITY__" not in body, conf.name
        assert 'sec_level = "authPriv"' in body, conf.name
        assert 'priv_password = "${SNMPV3_PRIV}"' in body, conf.name


def test_auth_no_priv_device_gets_its_own_inputs(monkeypatch, tmp_path):
    """Telegraf has one sec_level per input. A device that cannot encrypt must
    be polled from a separate authNoPriv input — never by lowering the rest."""
    devices = [
        Device("cr1", "ios_xe", "10.0.0.11", "hq", "core-wan"),
        Device("sw1", "ios_xe", "10.0.0.31", "hq", "core-dc", security="auth_no_priv"),
    ]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    assert mod.main() == 0

    produced = {p.name for p in tmp_path.glob("*.conf")}
    assert produced == {
        "snmp-interfaces.conf", "snmp-ios_xe.conf",
        "snmp-interfaces-authnopriv.conf", "snmp-ios_xe-authnopriv.conf",
    }
    for name in ("snmp-interfaces.conf", "snmp-ios_xe.conf"):
        body = (tmp_path / name).read_text()
        assert '"udp://10.0.0.11:161"' in body and "10.0.0.31" not in body
        assert 'sec_level = "authPriv"' in body
    for name in ("snmp-interfaces-authnopriv.conf", "snmp-ios_xe-authnopriv.conf"):
        body = (tmp_path / name).read_text()
        assert '"udp://10.0.0.31:161"' in body and "10.0.0.11" not in body
        assert 'sec_level = "authNoPriv"' in body
        assert "priv_" not in body, "authNoPriv must not carry a privacy protocol or password"
    # Same identity table either way — labels do not depend on the level.
    assert json.loads((tmp_path / "devices.json").read_text())["sw1"]["role"] == "core-dc"


def test_schema_security_levels_match_the_renderer(monkeypatch, tmp_path):
    """A level in the schema the renderer cannot write would skip the device."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    schema = yaml.safe_load((ROOT / "source-of-truth/schema/darqcube.yml").read_text())
    device = next(n for n in schema["nodes"] if n["name"] == "Device")
    attr = next(a for a in device["attributes"] if a["name"] == "snmp_security")
    assert {c["name"] for c in attr["choices"]} == set(mod.SECURITY)
    assert attr["default_value"] == mod.DEFAULT_SECURITY


def test_memory_kind_reaches_the_config(monkeypatch, tmp_path):
    """The unit each vendor reports must survive into the collector as a tag,
    or the Prometheus recording rule cannot tell bytes from percent."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    mod.main()
    assert 'memory_kind = "percent"' in (tmp_path / "snmp-vrp.conf").read_text()
    assert 'memory_kind = "used_free"' in (tmp_path / "snmp-ios_xe.conf").read_text()
    assert 'memory_kind = "used_total"' in (tmp_path / "snmp-routeros.conf").read_text()


def test_gnmi_device_is_not_also_polled_by_snmp(monkeypatch, tmp_path):
    """telemetry_mode is exclusive — collecting both would double-count."""
    devices = [Device("cr1", "ios_xe", "10.0.0.11", "hq", "core", mode="gnmi")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert (tmp_path / "gnmi.conf").exists()
    assert not (tmp_path / "snmp-interfaces.conf").exists()


def test_gnmi_on_unsupported_platform_warns_and_skips(monkeypatch, tmp_path, capsys):
    """RouterOS has no gNMI. Asking for it must be loud, not silently empty."""
    devices = [Device("mt-01", "routeros", "10.0.0.31", "hq", "wan", mode="gnmi")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    err = capsys.readouterr().err
    assert "does not support gNMI" in err
    assert not (tmp_path / "gnmi.conf").exists()


def test_unknown_platform_warns_and_skips(monkeypatch, tmp_path, capsys):
    devices = [Device("x1", "ios_xr", "10.0.0.99", "hq", "core")]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert "has no entry in platforms.yml" in capsys.readouterr().err


def test_schema_platforms_match_platforms_yml():
    """A platform in one place and not the other means a device can be seeded
    that no collector will ever poll — and it fails silently."""
    schema = yaml.safe_load((ROOT / "source-of-truth/schema/darqcube.yml").read_text())
    device = next(n for n in schema["nodes"] if n["name"] == "Device")
    attr = next(a for a in device["attributes"] if a["name"] == "platform")
    assert {c["name"] for c in attr["choices"]} == set(
        yaml.safe_load((ROOT / "platforms.yml").read_text())
    )


def test_no_schema_name_is_shorter_than_three_characters():
    """Infrahub rejects the whole schema load with string_too_short for an
    attribute or relationship name under 3 characters (e.g. os, ip)."""
    schema = yaml.safe_load((ROOT / "source-of-truth/schema/darqcube.yml").read_text())
    for node in schema["nodes"]:
        for item in node.get("attributes", []) + node.get("relationships", []):
            assert len(item["name"]) >= 3, f"{node[name]}.{item[name]} is too short for Infrahub"


def test_no_schema_description_hits_the_128_char_limit():
    """Infrahub rejects the ENTIRE schema load with string_too_long and does
    not name the field, so this is checked here instead of by bisecting."""
    text = (ROOT / "source-of-truth/schema/darqcube.yml").read_text()
    schema = yaml.safe_load(text)
    too_long = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str) and len(value) >= 128:
                    too_long.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    assert not too_long, f"descriptions >=128 chars: {too_long}"


# --- where the output lives, and who can read it --------------------------
# Found by cloning fresh from GitHub. The renderer wrote into a Docker NAMED
# volume, while test_wiring.py, verify.sh and seven docs all read the host
# directory observability/telegraf/generated/ — which nothing ever wrote to.
# A perfect install would have failed its own step 6 verification.

def test_all_three_containers_share_the_documented_host_directory():
    """infrahub-server writes, Telegraf and Logstash read — the same HOST path
    the docs, tests and verify.sh describe, not a named volume none can see."""
    services = {}
    for path in (ROOT / "compose").glob("*.yaml"):
        services.update((yaml.safe_load(path.read_text()) or {}).get("services", {}) or {})

    def mounts_for(svc, target):
        for spec in services[svc].get("volumes", []):
            src, _, rest = spec.partition(":")
            if rest.split(":")[0] == target:
                return src
        return None

    expected = "../observability/telegraf/generated"
    assert mounts_for("infrahub-server", "/generated") == expected
    assert mounts_for("telegraf", "/etc/telegraf/telegraf.d/generated") == expected
    assert mounts_for("logstash", "/usr/share/logstash/lookup") == expected


def test_rendered_files_are_world_readable_even_under_a_strict_umask(monkeypatch, tmp_path):
    """The renderer runs as root; Logstash reads as uid 1000. Under a 077 umask
    a plain open() gives 0600, which uid 1000 cannot read on Linux — every log
    line would then arrive labelled `unknown`, with no error anywhere. Verified
    on a real Linux filesystem; macOS bind mounts do not enforce it, which is
    why this is a unit test and not left to a dev box to notice."""
    import os
    import stat

    old = os.umask(0o077)
    try:
        mod = load_renderer(monkeypatch, THREE, tmp_path)
        assert mod.main() == 0
    finally:
        os.umask(old)

    for f in tmp_path.iterdir():
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode & stat.S_IROTH, f"{f.name} is {oct(mode)} — not readable by Logstash (uid 1000)"


def test_the_output_directories_exist_in_a_fresh_clone():
    """Both are gitignored except a .gitkeep. Without it the directory is
    absent after `git clone`, and `make config-get` tees into a path that does
    not exist yet."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    if not tracked:
        pytest.skip("not a git checkout")
    for keep in ("observability/telegraf/generated/.gitkeep", "automation/configs/.gitkeep"):
        assert keep in tracked.splitlines(), f"{keep} is not committed"


# --- make clean -------------------------------------------------------------
# `down -v` removes named volumes only. The two folders a container writes
# into the repo are bind mounts, so `make clean` has to empty them itself —
# otherwise the last install's rendered config keeps driving the collectors.

WRITTEN = ("observability/telegraf/generated", "automation/configs")


def _clean_sandbox(tmp_path):
    (tmp_path / "Makefile").write_text((ROOT / "Makefile").read_text())
    for d in WRITTEN:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / ".gitkeep").touch()
        (tmp_path / d / "leftover.json").write_text("{}")
    return tmp_path


def _make_clean(cwd, answer, compose="true"):
    import subprocess
    return subprocess.run(["make", "-s", "clean", f"COMPOSE={compose}"], cwd=cwd,
                          input=answer, capture_output=True, text=True)


def test_make_clean_empties_both_written_folders_but_keeps_gitkeep(tmp_path):
    cwd = _clean_sandbox(tmp_path)
    r = _make_clean(cwd, "y\n")
    assert r.returncode == 0, r.stderr
    for d in WRITTEN:
        assert sorted(p.name for p in (cwd / d).iterdir()) == [".gitkeep"], d


def test_make_clean_declined_deletes_nothing(tmp_path):
    cwd = _clean_sandbox(tmp_path)
    r = _make_clean(cwd, "n\n")
    assert "cancelled" in r.stdout
    for d in WRITTEN:
        assert (cwd / d / "leftover.json").exists(), d


def test_make_clean_deletes_nothing_if_compose_down_fails(tmp_path):
    cwd = _clean_sandbox(tmp_path)
    r = _make_clean(cwd, "y\n", compose="false")
    assert r.returncode != 0
    for d in WRITTEN:
        assert (cwd / d / "leftover.json").exists(), d


def test_make_clean_targets_the_directories_compose_writes_to():
    """CLEAN_DIRS must name the same folders compose bind-mounts read-write."""
    make = (ROOT / "Makefile").read_text()
    clean_dirs = set(re.search(r"^CLEAN_DIRS := (.+)$", make, re.M).group(1).split())
    assert clean_dirs == set(WRITTEN)


def test_snmp_templates_never_use_name_override():
    """name_override renames every metric an input emits, tables included:
    interface_oper_status arrived as device_oper_status, so every interface
    alert and dashboard panel matched nothing. Verified against a real device
    with Telegraf 1.34 — `name` renames only the top-level fields."""
    for tmpl in (ROOT / "observability/telegraf/profiles").glob("*.tmpl"):
        assert not re.search(r"^\s*name_override\s*=", tmpl.read_text(), re.M), tmpl.name


def test_metric_names_the_rules_and_dashboards_use_are_produced():
    """Every interface_<field> the alert rules and dashboards query must be a
    field of the `interface` table, or it silently returns nothing."""
    tmpl = (ROOT / "observability/telegraf/profiles/_interfaces.conf.tmpl").read_text()
    table = tmpl.split('name = "interface"', 1)[1]
    fields = set(re.findall(r'^\s+name = "([a-z_]+)"', table, re.M))
    used = set()
    for path in [*(ROOT / "observability/prometheus").rglob("*.yml"),
                 *(ROOT / "observability/grafana").rglob("*.json")]:
        used |= set(re.findall(r"\binterface_([a-z_]+)", path.read_text()))
    assert used, "no interface_* metric referenced — test is not looking in the right place"
    assert used <= fields, f"queried but never produced: {sorted(used - fields)}"


def test_identity_table_exists_before_the_collectors_start():
    """Telegraf refuses to start without devices.json (processors.lookup fails
    on a missing file), and a fresh install renders nothing — the repo ships no
    inventory. config-init creates an empty table first, and never replaces a
    rendered one; Telegraf and Logstash wait for it."""
    services = yaml.safe_load((ROOT / "compose/observability.yaml").read_text())["services"]
    init = services["config-init"]
    assert "../observability/telegraf/generated:/generated" in init["volumes"]
    script = init["command"][-1]
    for f in ("devices.json", "devices.yml"):
        assert f in script
    assert '[ ! -e "/generated/$$f" ]' in script, "must only create a missing table"
    for svc in ("telegraf", "logstash"):
        dep = services[svc]["depends_on"]["config-init"]
        assert dep["condition"] == "service_completed_successfully", svc


# --- dedicated NetFlow listeners (exporters behind NAT) ---------------------

def test_flow_port_renders_a_tagged_listener_per_device(monkeypatch, tmp_path):
    """Behind NAT every exporter shares one source address, so the listener port
    is the identity: one input per device, tagged with its name."""
    devices = [
        Device("edge-01", "ios_xe", "10.0.0.11", "branch-01", "wan", flow=True, flow_port=12057),
        Device("edge-02", "ios_xe", "10.0.0.12", "branch-02", "wan", flow=True, flow_port=12056),
        Device("core-01", "ios_xe", "10.0.0.21", "hq", "core", flow=True),   # shared listener
    ]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    assert mod.main() == 0
    body = (tmp_path / "netflow-dedicated.conf").read_text()
    assert body.count("[[inputs.netflow]]") == 2
    assert body.index("udp://:12056") < body.index("udp://:12057"), "sorted by port"
    assert 'service_address = "udp://:12057"' in body and 'flow_exporter = "edge-01"' in body
    assert 'flow_exporter = "edge-02"' in body and "core-01" not in body
    # The tag value must be a key of the identity table, or the lookup finds nothing.
    identity = json.loads((tmp_path / "devices.json").read_text())
    assert identity["edge-01"]["site"] == "branch-01"


def test_no_dedicated_file_without_flow_ports(monkeypatch, tmp_path):
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0
    assert not (tmp_path / "netflow-dedicated.conf").exists()


def test_stale_dedicated_listeners_are_removed(monkeypatch, tmp_path):
    """Dropping the last flow_port must also drop its listener file."""
    (tmp_path / "netflow-dedicated.conf").write_text("stale")
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0
    assert not (tmp_path / "netflow-dedicated.conf").exists()


def test_flow_port_without_flow_enabled_warns(monkeypatch, tmp_path, capsys):
    devices = [Device("edge-01", "ios_xe", "10.0.0.11", "b1", "wan", flow=False, flow_port=12056)]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert "flow_enabled is false" in capsys.readouterr().err
    assert not (tmp_path / "netflow-dedicated.conf").exists()


def test_duplicate_flow_port_warns_and_keeps_the_first(monkeypatch, tmp_path, capsys):
    devices = [
        Device("edge-01", "ios_xe", "10.0.0.11", "b1", "wan", flow=True, flow_port=12056),
        Device("edge-02", "ios_xe", "10.0.0.12", "b2", "wan", flow=True, flow_port=12056),
    ]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert "already used by edge-01" in capsys.readouterr().err
    assert "edge-02" not in (tmp_path / "netflow-dedicated.conf").read_text()


def test_flow_port_outside_the_published_range_warns(monkeypatch, tmp_path, capsys):
    """A listener on a port compose does not publish would receive nothing."""
    monkeypatch.setenv("FLOW_DEDICATED_FIRST", "12056")
    monkeypatch.setenv("FLOW_DEDICATED_LAST", "12060")
    devices = [Device("edge-01", "ios_xe", "10.0.0.11", "b1", "wan", flow=True, flow_port=13000)]
    mod = load_renderer(monkeypatch, devices, tmp_path)
    mod.main()
    assert "outside the published range 12056-12060" in capsys.readouterr().err
    assert not (tmp_path / "netflow-dedicated.conf").exists()


def test_flow_lookup_prefers_the_dedicated_listener_tag():
    """The NAT's source address must not win over the listener's device tag."""
    body = (ROOT / "observability/telegraf/conf.d/outputs.conf").read_text()
    assert """key = '{{ or (.Tag "flow_exporter") (.Tag "source") }}'""" in body


def test_compose_publishes_the_dedicated_flow_range():
    body = (ROOT / "compose/observability.yaml").read_text()
    assert "${FLOW_DEDICATED_FIRST:-2056}-${FLOW_DEDICATED_LAST:-2105}:" in body


# --- application labels for flows -------------------------------------------

class Host:
    def __init__(self, name, address, status="active"):
        self.name = Attr(name)
        self.address = Attr(address)
        self.status = Attr(status)


class App:
    def __init__(self, name, criticality="medium"):
        self.name = Attr(name)
        self.criticality = Attr(criticality)


class One:
    def __init__(self, peer):
        self.peer = peer


class Service:
    def __init__(self, name, host, app, port, protocol="tcp"):
        self.name = Attr(name)
        self.host = One(host)
        self.application = One(app)
        self.port = Attr(port)
        self.protocol = Attr(protocol)


def test_service_tables_key_the_server_side_of_both_directions(monkeypatch, tmp_path):
    db = Host("srv-db-01", "10.0.10.12/32")
    erp = App("erp", "critical")
    services = [Service("db-pg", db, erp, 5432), Service("dns-udp", Host("dns-01", "10.0.10.13"), App("dns"), 53, "udp")]
    mod = load_renderer(monkeypatch, THREE, tmp_path, services)
    assert mod.main() == 0
    dst = json.loads((tmp_path / "services-dst.json").read_text())
    src = json.loads((tmp_path / "services-src.json").read_text())
    assert dst["10.0.10.12:5432/tcp"] == {"application": "erp", "criticality": "critical"}
    assert src["10.0.10.12:5432/tcp"] == {"reply_application": "erp", "reply_criticality": "critical"}
    assert dst["10.0.10.13:53/udp"]["application"] == "dns", "/32 stripped, protocol kept"


def test_service_tables_exist_even_when_nothing_is_modelled(monkeypatch, tmp_path):
    """processors.lookup refuses to start on a missing file."""
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0
    for name in ("services-dst.json", "services-src.json"):
        assert json.loads((tmp_path / name).read_text()) == {}


def test_service_on_a_host_without_address_or_inactive_is_skipped(monkeypatch, tmp_path, capsys):
    services = [Service("a", Host("h1", None), App("x"), 80),
                Service("b", Host("h2", "10.0.0.2", status="maintenance"), App("y"), 80)]
    mod = load_renderer(monkeypatch, THREE, tmp_path, services)
    assert mod.main() == 0
    assert json.loads((tmp_path / "services-dst.json").read_text()) == {}
    assert "has no address" in capsys.readouterr().err






def test_flow_queries_use_the_metric_name_prometheus_stores():
    """The aggregator's flow_bytes_total field is exposed as
    netflow_flow_bytes_total. The bare name matched nothing, so the
    NoFlowsReceived alert fired forever and the flow panel stayed empty."""
    paths = [*(ROOT / "observability/prometheus").rglob("*.yml"),
             *(ROOT / "observability/grafana").rglob("*.json"),
             ROOT / "mcp/servers/prometheus.py"]
    for path in paths:
        text = path.read_text()
        bare = re.findall(r"(?<![a-z_])flow_(?:bytes|packets)_total", text)
        assert not bare, f"{path.name}: queries flow_*_total without the netflow_ prefix"

def test_static_flow_config_keeps_labels_bounded_and_references_no_generated_file():
    """conf.d is live the moment it is pulled; a lookup table it named would not
    exist until make render, and Telegraf refuses to start on a missing file."""
    conf = (ROOT / "observability/telegraf/conf.d/netflow.conf").read_text()
    assert "services-dst.json" not in conf and "services-src.json" not in conf
    assert 'tag = ["protocol", "direction"]' in conf
    taginclude = re.search(r"taginclude = \[(.*?)\]", conf).group(1)
    for label in ("application", "criticality", "protocol", "direction"):
        assert f'"{label}"' in taginclude
    for banned in ("src", "dst", "src_port", "dst_port"):
        assert f'"{banned}"' not in taginclude, "a per-flow value became a label"
    assert 'fieldpass = ["in_bytes", "in_packets"]' in conf


def test_application_processors_render_only_with_services(monkeypatch, tmp_path):
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0
    assert not (tmp_path / "netflow-applications.conf").exists()

    services = [Service("db-pg", Host("srv-db-01", "10.0.10.12"), App("erp", "critical"), 5432)]
    mod = load_renderer(monkeypatch, THREE, tmp_path, services)
    assert mod.main() == 0
    body = (tmp_path / "netflow-applications.conf").read_text()
    assert body.count("[[processors.lookup]]") == 2 and "[[processors.starlark]]" in body
    assert '{{.Field "dst"}}:{{.Field "dst_port"}}/{{.Tag "protocol"}}' in body
    assert "services-dst.json" in body and "services-src.json" in body
    orders = [int(n) for n in re.findall(r"order = (\d+)", body)]
    static = (ROOT / "observability/telegraf/conf.d/netflow.conf").read_text()
    converter = int(re.search(r"\[\[processors.converter\]\]\s*\n\s*order = (\d+)", static).group(1))
    override = int(re.search(r"\[\[processors.override\]\]\s*\n\s*order = (\d+)", static).group(1))
    assert all(converter < n < override for n in orders), "lookups must run after protocol is a tag, before addresses go"


def test_dropping_the_last_service_removes_the_processors(monkeypatch, tmp_path):
    (tmp_path / "netflow-applications.conf").write_text("stale")
    mod = load_renderer(monkeypatch, THREE, tmp_path)
    assert mod.main() == 0
    assert not (tmp_path / "netflow-applications.conf").exists()


def test_snmp_timing_comes_from_env_with_a_bounded_worst_case():
    """Hardcoded 10s x 1 retry failed a device on two lost UDP replies.
    The timing is per deployment now; the default keeps a dead device's cost
    at the 20 s docs/scale.md is sized for."""
    for tmpl in ("_interfaces.conf.tmpl", "_resources.conf.tmpl"):
        body = (ROOT / "observability/telegraf/profiles" / tmpl).read_text()
        # The :- defaults matter: a make render before make up (no env yet) must still load.
        assert 'timeout = "${SNMP_TIMEOUT:-5s}"' in body and "retries = ${SNMP_RETRIES:-3}" in body, tmpl
    env = yaml.safe_load((ROOT / "compose/observability.yaml").read_text())["services"]["telegraf"]["environment"]
    timeout = int(re.match(r"\$\{SNMP_TIMEOUT:-(\d+)s\}", env["SNMP_TIMEOUT"]).group(1))
    retries = int(re.match(r"\$\{SNMP_RETRIES:-(\d+)\}", env["SNMP_RETRIES"]).group(1))
    assert retries >= 3, "fewer retries fails a device on ordinary UDP loss"
    assert timeout * (retries + 1) <= 20, "a dead device would hold its shard longer than scale.md allows"
    example = (ROOT / ".env.example").read_text()
    assert f"SNMP_TIMEOUT={timeout}s" in example and f"SNMP_RETRIES={retries}" in example


DASHBOARDS = ROOT / "observability/grafana/provisioning/dashboards/darqcube"


def test_dashboards_are_valid_and_point_at_provisioned_datasources():
    """A wrong datasource uid or a duplicate dashboard uid fails silently in
    Grafana: the panel just says No data, or one dashboard hides another."""
    datasources = set()
    for f in (ROOT / "observability/grafana/provisioning/datasources").glob("*.y*ml"):
        datasources |= {d["uid"] for d in yaml.safe_load(f.read_text())["datasources"]}
    uids = []
    for f in DASHBOARDS.glob("*.json"):
        d = json.loads(f.read_text())
        uids.append(d["uid"])
        assert d["uid"].startswith("darqcube-"), f.name
        for p in d["panels"]:
            ds = p.get("datasource", {}).get("uid")
            assert ds in datasources, f"{f.name}: panel '{p['title']}' uses unknown datasource {ds}"
        for v in d.get("templating", {}).get("list", []):
            if v.get("type") == "query":
                assert v["datasource"]["uid"] in datasources, f"{f.name}: variable {v['name']}"
    assert len(uids) == len(set(uids)), f"duplicate dashboard uid: {uids}"
    assert {"darqcube-network", "darqcube-devices", "darqcube-netflow", "darqcube-applications", "darqcube-logs"} <= set(uids)


def test_grafana_opens_on_a_shipped_dashboard():
    env = yaml.safe_load((ROOT / "compose/observability.yaml").read_text())["services"]["grafana"]["environment"]
    home = env["GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"]
    assert home.startswith("/etc/grafana/provisioning/dashboards/darqcube/")
    assert (DASHBOARDS / home.rsplit("/", 1)[1]).exists(), "home dashboard file is not shipped"


def test_every_loki_panel_has_a_non_empty_matcher():
    """{device=~"$device"} alone fails in Loki once the variable expands to .*
    — the panel shows nothing and only the query inspector says why."""
    for f in DASHBOARDS.glob("*.json"):
        for p in json.loads(f.read_text())["panels"]:
            if p.get("datasource", {}).get("type") != "loki":
                continue
            for target in p.get("targets", []):
                assert 'device=~".+"' in target["expr"], f"{f.name}: '{p['title']}' has no non-empty matcher"

