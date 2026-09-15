#!/usr/bin/env python3
"""Apply source-of-truth/devices/*.yml to Infrahub.

Idempotent: re-running updates existing nodes instead of duplicating them, so
this is the normal way to change a device, not just to create one.

Runs inside the infrahub-server container (which already has the SDK):
    make seed
"""
from __future__ import annotations

import os
import sys

import yaml
from infrahub_sdk import Config, InfrahubClientSync

INFRAHUB_URL = os.environ.get("INFRAHUB_ADDRESS", "http://infrahub-server:8000")
TOKEN = os.environ.get("INFRAHUB_API_TOKEN") or os.environ.get("INFRAHUB_INITIAL_ADMIN_TOKEN")
BRANCH = os.environ.get("INFRAHUB_BRANCH", "main")

SITES_FILE = os.environ.get("SITES_FILE", "/devices/sites.yml")
DEVICES_FILE = os.environ.get("DEVICES_FILE", "/devices/devices.yml")
PLATFORMS_FILE = os.environ.get("PLATFORMS_FILE", "/platforms.yml")


def load(path: str) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def upsert(client: InfrahubClientSync, kind: str, key: str, attrs: dict):
    """Create the node, or update it in place if it already exists.

    Returns (node, "created"|"updated").
    """
    existing = client.filters(kind=kind, branch=BRANCH, name__value=key)
    if existing:
        node = existing[0]
        for field, value in attrs.items():
            if field == "name":
                continue
            setattr(node, field, value)
        node.save()
        return node, "updated"

    node = client.create(kind=kind, branch=BRANCH, **attrs)
    node.save()
    return node, "created"


def main() -> int:
    if not TOKEN:
        print("!! INFRAHUB_API_TOKEN not set", file=sys.stderr)
        return 2

    platforms = load(PLATFORMS_FILE)
    sites_doc = load(SITES_FILE)
    devices_doc = load(DEVICES_FILE)

    client = InfrahubClientSync(address=INFRAHUB_URL, config=Config(api_token=TOKEN))

    # --- sites first: a device's site relationship is required -------------
    site_ids: dict[str, str] = {}
    for site in sites_doc.get("sites", []):
        node, action = upsert(
            client,
            "NetworkSite",
            site["name"],
            {"name": site["name"], "description": site.get("description")},
        )
        site_ids[site["name"]] = node.id
        print(f"  site   {site['name']:<14} {action}")

    # --- devices -----------------------------------------------------------
    errors = []
    for dev in devices_doc.get("devices", []):
        name = dev["name"]

        # Validate before touching Infrahub, so a typo fails loudly and early
        # rather than creating a device no collector will ever poll.
        if dev["site"] not in site_ids:
            errors.append(f"{name}: site '{dev['site']}' is not in sites.yml")
            continue
        if dev["platform"] not in platforms:
            errors.append(
                f"{name}: platform '{dev['platform']}' is not in platforms.yml "
                f"(have: {', '.join(sorted(platforms))})"
            )
            continue

        _, action = upsert(
            client,
            "NetworkDevice",
            name,
            {
                "name": name,
                "role": dev["role"],
                "platform": dev["platform"],
                "management_ip": dev["management_ip"],
                "status": dev.get("status", "active"),
                "telemetry_mode": dev.get("telemetry_mode", "snmp"),
                "flow_enabled": dev.get("flow_enabled", False),
                "description": dev.get("description"),
                "site": site_ids[dev["site"]],
            },
        )
        print(f"  device {name:<14} {action}")

    if errors:
        print("\n!! not seeded:", file=sys.stderr)
        for err in errors:
            print(f"   {err}", file=sys.stderr)
        return 1

    total = len(devices_doc.get("devices", []))
    print(f"\nseeded {len(site_ids)} sites, {total} devices — now run: make render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
