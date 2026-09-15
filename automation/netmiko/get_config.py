#!/usr/bin/env python3
"""Standalone: fetch the running configuration of one device, or all of them.

    python -m automation.netmiko.get_config --device cr1
    python -m automation.netmiko.get_config --all

Configs are archived under automation/configs/<device>.cfg either way.
"""
from __future__ import annotations

import argparse
import sys

from automation.nornir import tasks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--device")
    group.add_argument("--all", action="store_true", help="every device in Infrahub")
    ap.add_argument("--quiet", action="store_true", help="save only, do not print")
    args = ap.parse_args()

    targets = [d["name"] for d in tasks.list_devices()] if args.all else [args.device]

    failed = 0
    for name in targets:
        try:
            result = tasks.get_config(name)
        except Exception as exc:
            print(f"!! {name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"{name}: {result['lines']} lines -> {result['saved_to']}", file=sys.stderr)
        if not args.quiet and not args.all:
            print(result["config"])

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
