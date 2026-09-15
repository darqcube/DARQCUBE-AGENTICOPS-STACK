#!/usr/bin/env python3
"""Standalone: fetch and parse operational state from one device.

    python -m automation.netmiko.get_state --device mt-01
    python -m automation.netmiko.get_state --device mt-01 --raw     # capture a sample

--raw prints the unparsed CLI output, which is exactly what you redirect into
automation/textfsm/samples/ when a template needs writing. See
docs/how-to/add-a-textfsm-template.md.

Uses the same functions as the HTTP API, so behaviour cannot drift between them.
"""
from __future__ import annotations

import argparse
import json
import sys

from automation.nornir import tasks
from automation.textfsm import parse


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", required=True)
    ap.add_argument("--command", help="override the platform's default state command")
    ap.add_argument("--raw", action="store_true", help="print unparsed output")
    args = ap.parse_args()

    try:
        result = tasks.get_state(args.device, command=args.command, raw=args.raw)
    except tasks.DeviceError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1
    except parse.ParseError as exc:
        print(f"!! {exc}", file=sys.stderr)
        print("   Re-run with --raw to capture a sample, then see "
              "docs/how-to/add-a-textfsm-template.md", file=sys.stderr)
        return 2

    if args.raw:
        print(result["raw"], end="")
    else:
        print(json.dumps(result["rows"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
