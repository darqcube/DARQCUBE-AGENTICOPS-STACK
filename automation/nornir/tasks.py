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
_device_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def device_lock(device: str) -> threading.Lock:
    with _locks_guard:
        return _device_locks.setdefault(device, threading.Lock())


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


def _build_nornir():
    """Build a Nornir instance from the live Infrahub inventory.

    NOTE: `group_mappings` is deliberately absent. The plugin tries to resolve
    relationship peers it never fetched into the SDK store, so a mapping like
    "site.name" yields None and slugify() raises TypeError before a single host
    loads. Grouping is done here, after init, instead.
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
                "schema_mappings": [
                    {"name": "hostname", "mapping": "management_ip.value"},
                    {"name": "platform", "mapping": "platform.value"},
                ],
            },
        },
        logging={"enabled": False},
    )

    plats = platforms()
    for host in nr.inventory.hosts.values():
        # management_ip is an IPHost and may carry a prefix; polling "10.0.0.1/24"
        # would fail with a name-resolution error rather than anything obvious.
        host.hostname = str(host.hostname or "").split("/")[0]
        infrahub_platform = host.platform
        host.data["infrahub_platform"] = infrahub_platform
        # Infrahub's platform dropdown -> the netmiko device_type.
        host.platform = plats.get(infrahub_platform, {}).get("netmiko_type", infrahub_platform)
        host.username = DEVICE_USER
        host.password = DEVICE_PASSWORD

    return nr


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

    Vendor-neutral: the same rules run for Cisco, Huawei and MikroTik, because
    the parsed rows are normalised to one shape first.
    """
    from automation.assurance import engine

    state = get_state(device)
    result = engine.run_rules(state["platform"], state["rows"])
    result["device"] = device
    result["command"] = state["command"]
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
