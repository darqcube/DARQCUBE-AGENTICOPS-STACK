"""Turn three vendors' parsed output into one shape.

This is the piece that makes vendor-neutral assurance possible. One mapping
layer over TextFSM output lets a single rule engine cover every platform, rather
than each rule carrying a branch per vendor.

The vendors disagree about everything except the concept:

    cisco_ios         interface, status, proto
                      status = "up" | "administratively down"
    huawei_vrp        interface, phy, protocol
                      phy = "up" | "down" | "*down" (admin down)
    mikrotik_routeros name, flags
                      flags: R = running, X = disabled, D = dynamic

Normalised shape, for every platform:

    {"interface": str, "admin_up": bool, "oper_up": bool, "raw": {...}}
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

PLATFORMS = Path(os.environ.get("PLATFORMS_FILE", Path(__file__).resolve().parents[2] / "platforms.yml"))


class NormaliseError(RuntimeError):
    pass


def _truthy_up(value: str) -> bool:
    return str(value).strip().lower() in {"up", "yes", "true", "1", "enabled"}


def _require(row: dict, *fields: str) -> None:
    """Fail loudly when the parser and the normaliser disagree on field names.

    Without this, a renamed field reads as "" and the booleans below come out
    plausible but WRONG — which is far worse than an error, because the result
    still looks like data. This was a real bug: the RouterOS flags live in
    `status`, not `flags`, and reading the wrong key reported every running
    interface as down.
    """
    missing = [f for f in fields if f not in row]
    if missing:
        raise NormaliseError(
            f"parsed row is missing {missing} — the TextFSM template and the "
            f"normaliser disagree about field names. Row has: {sorted(row)}"
        )


def _cisco(row: dict) -> dict:
    _require(row, "interface", "status", "proto")
    status = str(row.get("status", "")).lower()
    proto = str(row.get("proto", "")).lower()
    return {
        "interface": row.get("interface", ""),
        # "administratively down" is the only admin-down form IOS emits here.
        "admin_up": "admin" not in status,
        "oper_up": _truthy_up(proto),
    }


def _vrp(row: dict) -> dict:
    _require(row, "interface", "phy", "protocol")
    phy = str(row.get("phy", "")).lower()
    protocol = str(row.get("protocol", "")).lower()
    return {
        "interface": row.get("interface", ""),
        # VRP marks an administratively-down port with a leading '*'.
        "admin_up": not phy.startswith("*"),
        "oper_up": _truthy_up(protocol),
    }


def _routeros(row: dict) -> dict:
    # RouterOS encodes state in single-letter flags rather than columns:
    #   R running   X disabled   D dynamic   S slave
    # ntc-templates puts those flags in `status`, NOT in a field called `flags`.
    _require(row, "name", "status")
    flags = str(row.get("status", "")).upper()
    return {
        "interface": row.get("name", ""),
        "admin_up": "X" not in flags,      # X = administratively disabled
        "oper_up": "R" in flags,           # R = running
    }


_MAP = {"ios_xe": _cisco, "vrp": _vrp, "routeros": _routeros}


def normalise_interfaces(platform: str, rows: list[dict]) -> list[dict]:
    """Parsed rows -> the common interface shape."""
    if platform not in _MAP:
        raise NormaliseError(
            f"no interface normaliser for platform '{platform}'. "
            f"Add one in automation/assurance/normalise.py — see "
            f"docs/how-to/add-a-platform.md. Have: {', '.join(sorted(_MAP))}"
        )
    fn = _MAP[platform]
    out = []
    for row in rows:
        item = fn(row)
        if not item["interface"]:
            continue          # header or separator line that survived parsing
        item["raw"] = row
        out.append(item)

    if not out:
        raise NormaliseError(
            f"{platform}: {len(rows)} parsed row(s) but none had an interface "
            f"name — the normaliser and the TextFSM template disagree about "
            f"field names."
        )
    return out


def supported_platforms() -> list[str]:
    return sorted(_MAP)
