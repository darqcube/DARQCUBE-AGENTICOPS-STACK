"""Build a pyATS testbed for one device, from the cached inventory.

No testbed YAML on disk: a file would drift from the source of truth the moment
a device changed, and would put device credentials in the repository. The
inventory is the same cached Nornir inventory the rest of automation uses, so
this costs no extra Infrahub query.
"""
from __future__ import annotations

import os

from automation.nornir import tasks

DEVICE_USER = os.environ.get("DEVICE_USER", "")
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "")


class NoPyatsSupport(RuntimeError):
    """The platform has no `pyats:` block in platforms.yml."""


def pyats_spec(platform: str) -> dict | None:
    """The `pyats:` block for a platform, or None if it has none."""
    return tasks.platforms().get(platform, {}).get("pyats")


def build_testbed(device: str):
    """A one-device pyATS testbed plus its spec, or NoPyatsSupport."""
    from pyats.topology import loader

    host = tasks.get_nornir(device).inventory.hosts[device]
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
                    }
                },
            }
        },
    }
    return loader.load(testbed), spec
