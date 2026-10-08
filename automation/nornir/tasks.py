"""Device access: get and put, over Nornir + Netmiko.

Inventory is LIVE from Infrahub through the nornir-infrahub plugin, so there is
no inventory file to drift. Credentials come from the environment and are never
modelled in Infrahub.

The same functions back both the HTTP API and the standalone scripts in
automation/netmiko/, so a capability is written once.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import yaml
from nornir import InitNornir
from nornir_netmiko.tasks import netmiko_send_command, netmiko_send_config

# Absolute import. `from textfsm import parse` would resolve to the INSTALLED
# textfsm library, not automation/textfsm/ — see automation/__init__.py.
from automation.textfsm import parse as textfsm_parse
from automation import hostkeys

INFRAHUB_URL = os.environ.get("INFRAHUB_URL", "http://infrahub-server:8000")
INFRAHUB_TOKEN = os.environ.get("INFRAHUB_API_TOKEN", "")
BRANCH = os.environ.get("INFRAHUB_BRANCH", "main")
DEVICE_USER = os.environ.get("DEVICE_USER", "")
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "")
CONCURRENCY = int(os.environ.get("AUTOMATION_CONCURRENCY", "8"))
PLATFORMS_FILE = Path(os.environ.get("PLATFORMS_FILE", "/app/platforms.yml"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/automation/configs"))

# Building a Nornir instance fetches the WHOLE inventory from Infrahub — at 400
# devices that is a 400-node GraphQL query. Doing it per operation meant a
# single config push cost four of them, plus four separate SSH logins to the
# same device, which real AAA and `login block-for` will lock out.
#
# The inventory is cached for a short TTL and reused. `make seed` changes it
# rarely, and 60s is short enough that a newly added device shows up while the
# operator is still typing the next command.
INVENTORY_TTL = int(os.environ.get("INVENTORY_CACHE_TTL", "60"))
_cache: dict = {"nr": None, "at": 0.0}
_cache_lock = threading.Lock()

# Caching the inventory means every caller gets a view over the SAME Host
# objects, and Nornir stores the open connection on the Host. Two concurrent
# requests for one device would therefore share one SSH session and interleave
# their CLI output into garbage — and one finishing would close the other's
# connection mid-command.
#
# One lock per device: the fleet still runs concurrently, a single device is
# serialised. This is also what a device wants; most will not accept many
# simultaneous sessions from one source anyway.
#
# REENTRANT, deliberately. Assurance holds the lock across its whole run — the
# TextFSM read AND the pyATS session — so the two never hit one device at once.
# The TextFSM read takes the same lock internally; with a plain Lock that
# re-acquisition from the same thread deadlocks. RLock lets the owning thread
# re-enter while still excluding every other thread.
_device_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def device_lock(device: str) -> threading.RLock:
    with _locks_guard:
        return _device_locks.setdefault(device, threading.RLock())


class DeviceError(RuntimeError):
    pass


def platforms() -> dict:
    with open(PLATFORMS_FILE) as fh:
        return yaml.safe_load(fh)


def get_nornir(device: str | None = None, fresh: bool = False):
    """A Nornir instance from the live Infrahub inventory, inventory cached.

    Pass `fresh=True` straight after a seed to bypass the cache.

    `.filter()` returns a view over the SAME host objects, so a connection
    opened through one view is reused by the next — which is what makes a
    config push a single SSH session rather than four.
    """
    with _cache_lock:
        stale = time.time() - _cache["at"] > INVENTORY_TTL
        if fresh or _cache["nr"] is None or stale:
            _cache["nr"] = _build_nornir()
            _cache["at"] = time.time()
        base = _cache["nr"]

    if device:
        filtered = base.filter(name=device)
        if not filtered.inventory.hosts:
            raise DeviceError(
                f"device '{device}' is not in Infrahub (branch {BRANCH}). "
                f"If you just added it, run `make seed`."
            )
        return filtered
    return base


def invalidate_inventory() -> None:
    """Drop the cached inventory — call after seeding."""
    with _cache_lock:
        _cache["nr"] = None
        _cache["at"] = 0.0


def management_address(node) -> str:
    """Where to SSH: management_host when set, else management_ip.

    Same rule as render-inventory.py's target_of(), so the collectors and
    automation always reach a device at the same address. management_ip is an
    IPHost and may carry a prefix; connecting to "10.0.0.1/24" fails with a
    name-resolution error rather than anything obvious.
    """
    def value(attr):
        return str(getattr(getattr(node, attr, None), "value", None) or "").strip()

    return value("management_host") or value("management_ip").split("/")[0]


def _build_nornir():
    """Build a Nornir instance from the live Infrahub inventory.

    NOTE: `group_mappings` is deliberately absent. The plugin tries to resolve
    relationship peers it never fetched into the SDK store, so a mapping like
    "site.name" yields None and slugify() raises TypeError before a single host
    loads. Grouping is done here, after init, instead.

    `hostname` is not a schema_mapping either: the address is management_host
    when set, else management_ip, and a mapping can name only one attribute.
    It would also break on an empty management_ip — the plugin converts IPHost
    with `value.ip`, and None raises AttributeError, failing the whole load.
    """
    nr = InitNornir(
        runner={"plugin": "threaded", "options": {"num_workers": CONCURRENCY}},
        inventory={
            "plugin": "InfrahubInventory",
            "options": {
                "address": INFRAHUB_URL,
                "token": INFRAHUB_TOKEN,
                "branch": BRANCH,
                "host_node": {"kind": "NetworkDevice"},
                # An ATTRIBUTE is mapped by its bare name. A dotted mapping must
                # start with a relationship: nornir-infrahub 1.2 rejects
                # "platform.value" and the whole inventory fails to load.
                "schema_mappings": [
                    {"name": "platform", "mapping": "platform"},
                ],
            },
        },
        logging={"enabled": False},
    )

    plats = platforms()
    for host in nr.inventory.hosts.values():
        host.hostname = management_address(host.data.get("InfrahubNode"))
        infrahub_platform = host.platform
        host.data["infrahub_platform"] = infrahub_platform
        # Infrahub's platform dropdown -> the netmiko device_type.
        host.platform = plats.get(infrahub_platform, {}).get("netmiko_type", infrahub_platform)
        host.username = DEVICE_USER
        host.password = DEVICE_PASSWORD
        _apply_host_keys(host)

    return nr


def _apply_host_keys(host) -> None:
    """Pin the device's SSH host keys from Infrahub, when it has any.

    Netmiko (paramiko) otherwise accepts whatever key a device presents. With
    keys pinned it checks them and refuses anything else. A value that does
    not parse fails CLOSED — no keys trusted, so no session — rather than
    falling back to accepting any key; one device's typo never breaks the
    inventory for the rest.
    """
    from nornir.core.inventory import ConnectionOptions

    raw = getattr(getattr(host.data.get("InfrahubNode"), "ssh_host_keys", None), "value", None)
    try:
        keys = hostkeys.parse(raw)
        host.data["ssh_host_keys_error"] = None
    except hostkeys.HostKeyError as exc:
        keys = []
        host.data["ssh_host_keys_error"] = f"ssh_host_keys in Infrahub: {exc}"
    host.data["ssh_host_keys"] = keys
    if not (keys or host.data["ssh_host_keys_error"]):
        return
    trust = hostkeys.trust_file(f"{host.name}.paramiko", keys,
                                [hostkeys.paramiko_name(str(host.hostname))])
    host.connection_options["netmiko"] = ConnectionOptions(extras={
        "ssh_strict": True, "system_host_keys": False,
        "alt_host_keys": True, "alt_key_file": str(trust),
    })


def _one(result, device: str):
    host_result = result[device]
    if host_result.failed:
        raise DeviceError(f"{device}: {host_result.exception or host_result[0].exception}")
    return host_result[0].result


def get_config(device: str, nr=None) -> dict:
    """Fetch the running configuration and archive a copy.

    Pass `nr` to reuse an existing session; the caller then owns the lock.
    """
    if nr is None:
        with device_lock(device):
            return _get_config(device, get_nornir(device), own=True)
    return _get_config(device, nr, own=False)


def _get_config(device: str, nr, own: bool) -> dict:
    platform = nr.inventory.hosts[device].data["infrahub_platform"]
    command = platforms()[platform]["show_run"]

    result = nr.run(task=netmiko_send_command, command_string=command, read_timeout=120)
    config = _one(result, device)

    if own:
        nr.close_connections()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / f"{device}.cfg"
    path.write_text(config)

    return {
        "device": device,
        "platform": platform,
        "command": command,
        "lines": len(config.splitlines()),
        "saved_to": str(path),
        "config": config,
    }


def get_state(device: str, command: str | None = None, raw: bool = False, nr=None) -> dict:
    """Operational state, parsed into rows by TextFSM.

    This is what makes 'get information from the device' return data rather
    than a wall of text — and it is what the MCP layer serves to an AI.

    Pass `nr` to reuse an existing session; the caller then owns the lock.
    """
    if nr is None:
        with device_lock(device):
            return _get_state(device, get_nornir(device), command, raw, own=True)
    return _get_state(device, nr, command, raw, own=False)


def _get_state(device: str, nr, command: str | None, raw: bool, own: bool) -> dict:
    platform = nr.inventory.hosts[device].data["infrahub_platform"]
    command = command or platforms()[platform]["state_cmd"]

    result = nr.run(task=netmiko_send_command, command_string=command, read_timeout=60)
    output = _one(result, device)

    if own:
        nr.close_connections()

    payload = {"device": device, "platform": platform, "command": command}
    if raw:
        payload["raw"] = output
        return payload

    # parse_output raises on an empty parse rather than returning [], so a
    # missing template is visible instead of looking like an idle device.
    # The raw output is returned alongside so a template gap never loses data.
    payload["rows"] = textfsm_parse.parse_output(platform, command, output)
    payload["raw"] = output
    return payload


def put_config(device: str, lines: list[str]) -> dict:
    """Push configuration lines, archiving the previous config first."""
    max_lines = int(os.environ.get("MAX_CONFIG_LINES", "200"))
    if not lines:
        raise DeviceError("no configuration lines supplied")
    if len(lines) > max_lines:
        raise DeviceError(f"{len(lines)} lines exceeds MAX_CONFIG_LINES={max_lines}")

    # ONE Nornir instance for all four device operations below. Each one used
    # to build its own, which meant four SSH logins in quick succession to the
    # same device — enough to trip AAA rate limiting or `login block-for` on
    # real hardware, and four full inventory fetches from Infrahub.
    # Serialised per device: a config push must not interleave with a
    # concurrent read on the same SSH session.
    with device_lock(device):
        return _put_config(device, lines)


def _put_config(device: str, lines: list[str]) -> dict:
    nr = get_nornir(device)
    try:
        # Archive before changing anything, so there is always something to
        # compare against and restore from.
        before_config = get_config(device, nr=nr)

        # Pre-change snapshot. Best-effort: a device with no TextFSM template
        # can still be configured, it just cannot be compared — and saying so
        # is better than refusing the change or pretending the check happened.
        before_state, state_error = None, None
        try:
            before_state = snapshot_device(device, nr=nr)["snapshot"]
        except Exception as exc:
            state_error = str(exc)

        result = nr.run(task=netmiko_send_config, config_commands=lines)
        output = _one(result, device)

        payload = {
            "device": device,
            "lines_sent": len(lines),
            "config_archived_to": before_config["saved_to"],
            "output": output,
        }

        # Post-change comparison, so a push reports what it actually changed
        # rather than only that it completed.
        if before_state:
            from automation.assurance import engine
            try:
                after_state = snapshot_device(device, nr=nr)["snapshot"]
                payload["state_change"] = engine.compare(before_state, after_state)
            except Exception as exc:
                payload["state_change"] = {"error": str(exc)}
        else:
            payload["state_change"] = {"skipped": state_error}

        return payload
    finally:
        # Devices time out idle sessions and a long-running API should not sit
        # on hundreds of them.
        nr.close_connections()


def run_assurance(device: str) -> dict:
    """Run the assurance rules against a device's live state.

    Two engines, one result:

      TextFSM   interface rules — every platform, through the normaliser
      pyATS     Genie-backed rules — only where platforms.yml declares a
                `pyats:` block, and only for the features it lists

    pyATS is additive: a platform is never assured by pyATS alone, so a gap in
    Genie's coverage cannot silently remove a platform's checks.

    The device lock is held across BOTH. pyATS opens its own SSH session through
    unicon, separate from Netmiko's; holding the lock keeps the two from ever
    being open on one device at once. The lock is reentrant, so get_state()
    taking it again from this thread is fine.
    """
    from automation.assurance import engine
    from automation.pyats import checks as pyats_checks
    from automation.pyats.testbed import pyats_spec

    with device_lock(device):
        state = get_state(device)
        features, feature_errors, absent, pyats_error = {}, {}, {}, None
        wanted = {r["feature"] for r in engine.load_rules() if r.get("source") == "pyats"}
        if pyats_spec(state["platform"]) and wanted:
            try:
                features, feature_errors, absent = pyats_checks.collect(device, wanted)
            except Exception as exc:
                # The session itself failed — reported on every pyATS rule; the
                # TextFSM results still stand.
                pyats_error = f"pyATS could not collect: {exc}"
        result = engine.run_rules(state["platform"], state["rows"], features, pyats_error,
                                  feature_errors, absent)

    result["device"] = device
    result["command"] = state["command"]
    return result


def pyats_features(device: str) -> dict:
    """What pyATS/Genie can return for this device's platform. No device session."""
    from automation.pyats.checks import features_of
    from automation.pyats.testbed import pyats_spec

    platform = get_nornir(device).inventory.hosts[device].data["infrahub_platform"]
    spec = pyats_spec(platform)
    found = features_of(spec)
    return {
        "device": device,
        "platform": platform,
        "supported": bool(found),
        "os": (spec or {}).get("os"),
        "features": found,
        **({} if found else {"reason": f"platform '{platform}' has no pyats block in "
                                       f"platforms.yml — use the TextFSM state instead"}),
    }


def pyats_learn(device: str, feature: str) -> dict:
    """Genie's structured view of one feature, from the platform's allow-list.

    Same session rules as run_assurance: unicon opens its own SSH session, so
    the device lock is held for the whole collection.
    """
    from automation.pyats import checks as pyats_checks

    known = pyats_features(device)
    if feature not in known["features"]:
        raise DeviceError(
            f"{device}: '{feature}' is not a pyATS feature for platform "
            f"'{known['platform']}'. Available: {sorted(known['features']) or 'none'}"
        )
    with device_lock(device):
        features, errors, absent = pyats_checks.collect(device, {feature})
    result = pyats_checks.feature_result(device, feature, features, errors, absent)
    result["platform"] = known["platform"]
    result["via"] = known["features"][feature]
    return result


def pyats_bgp_neighbors(device: str) -> dict:
    """Every BGP session as {vrf, af, peer, state} — compact, any Genie OS."""
    from automation.pyats import checks as pyats_checks

    result = pyats_learn(device, "bgp")
    if result["status"] == "ok":
        result["sessions"] = pyats_checks.bgp_peers(result.pop("data"))
    return result


def snapshot_device(device: str, nr=None) -> dict:
    """A comparable point-in-time view of a device, for pre/post comparison."""
    from automation.assurance import engine

    state = get_state(device, nr=nr)
    return {
        "device": device,
        "platform": state["platform"],
        "snapshot": engine.snapshot(state["platform"], state["rows"]),
    }


def get_config_structured(device: str, kind: str = "interfaces") -> dict:
    """Running config parsed into structure with TTP.

    TextFSM handles tabular `show` output; config is hierarchical, which is
    what TTP is for.
    """
    from automation.ttp import parse as ttp_parse

    config = get_config(device)
    rows = ttp_parse.parse_config(config["platform"], config["config"], kind=kind)
    return {
        "device": device,
        "platform": config["platform"],
        "kind": kind,
        "rows": rows,
    }


def list_devices() -> list[dict]:
    """Every device Infrahub knows about, as the automation layer sees it."""
    nr = get_nornir()
    return [
        {
            "name": name,
            "hostname": host.hostname,
            "platform": host.data["infrahub_platform"],
            "netmiko_type": host.platform,
        }
        for name, host in nr.inventory.hosts.items()
    ]
