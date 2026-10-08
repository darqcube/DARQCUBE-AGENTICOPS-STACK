"""Build a pyATS testbed for one device, from the cached inventory.

No testbed YAML on disk: a file would drift from the source of truth the moment
a device changed, and would put device credentials in the repository. The
inventory is the same cached Nornir inventory the rest of automation uses, so
this costs no extra Infrahub query.
"""
from __future__ import annotations

import os

DEVICE_USER = os.environ.get("DEVICE_USER", "")
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "")

# SSH host keys. When Infrahub holds the device's keys (`ssh_host_keys`, set by
# `make pin-host-keys` — see automation/hostkeys.py), they are the ONLY keys
# accepted: StrictHostKeyChecking=yes against a file generated from Infrahub.
#
# Otherwise they are learned once and remembered by DEVICE NAME (the
# source-of-truth identity), not by address. pyATS runs the system ssh, which by default files a key under the
# IP it connected to — so when addresses move (a lab platform reshuffling them
# on restart, DHCP), a device's unchanged key is filed under another device's
# old IP and ssh refuses with "REMOTE HOST IDENTIFICATION HAS CHANGED".
# Keyed by name, an address change is irrelevant; a genuinely changed key on a
# known device is still refused. A device never seen before is learned once
# (accept-new). The file lives under configs/ — bind-mounted, gitignored —
# so learned keys survive container rebuilds.
KNOWN_HOSTS = os.environ.get("PYATS_KNOWN_HOSTS", "/app/automation/configs/known_hosts")


def ssh_options(device: str, pinned: str | None = None) -> str:
    """ssh options for one device's pyATS session.

    `pinned`: a known_hosts file generated from the keys in Infrahub. With it,
    only those keys are accepted and nothing is learned.
    """
    if pinned:
        return (f"-o HostKeyAlias={device} -o StrictHostKeyChecking=yes "
                f"-o UserKnownHostsFile={pinned} -o GlobalKnownHostsFile=/dev/null")
    return (f"-o HostKeyAlias={device} -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={KNOWN_HOSTS}")


class NoPyatsSupport(RuntimeError):
    """The platform has no `pyats:` block in platforms.yml."""


def pyats_spec(platform: str) -> dict | None:
    """The `pyats:` block for a platform, or None if it has none."""
    from automation.nornir import tasks   # lazy: keeps ssh_options() testable offline

    return tasks.platforms().get(platform, {}).get("pyats")


def build_testbed(device: str):
    """A one-device pyATS testbed plus its spec, or NoPyatsSupport."""
    from pyats.topology import loader

    from automation import hostkeys
    from automation.nornir import tasks

    host = tasks.get_nornir(device).inventory.hosts[device]
    if host.data.get("ssh_host_keys_error"):
        # Fail closed: a broken pin must never fall back to learning a key.
        raise tasks.DeviceError(f"{device}: {host.data['ssh_host_keys_error']}")
    keys = host.data.get("ssh_host_keys") or []
    pinned = str(hostkeys.trust_file(f"{device}.ssh", keys, [device])) if keys else None
    platform = host.data["infrahub_platform"]
    spec = pyats_spec(platform)
    if not spec:
        raise NoPyatsSupport(
            f"{device}: platform '{platform}' has no pyats block in platforms.yml. "
            f"Its assurance runs on TextFSM only."
        )

    testbed = {
        "testbed": {
            "name": "darqcube",
            "credentials": {"default": {"username": DEVICE_USER, "password": DEVICE_PASSWORD}},
        },
        "devices": {
            device: {
                "os": spec["os"],
                "type": "router",
                "connections": {
                    "cli": {
                        "protocol": "ssh",
                        "ip": str(host.hostname).split("/")[0],
                        "ssh_options": ssh_options(device, pinned),
                    }
                },
            }
        },
    }
    return loader.load(testbed), spec
