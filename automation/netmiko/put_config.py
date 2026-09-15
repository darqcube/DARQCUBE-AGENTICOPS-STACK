#!/usr/bin/env python3
"""Standalone: push configuration lines to one device.

    python -m automation.netmiko.put_config --device cr1 --file change.txt
    echo "interface Lo99" | python -m automation.netmiko.put_config --device cr1 -

The running config is archived to automation/configs/<device>.cfg BEFORE
anything is sent, so there is always something to compare against and restore
from. Blank lines and comments (#, !) are stripped.
"""
from __future__ import annotations

import argparse
import sys

from automation.nornir import tasks


def read_lines(source: str) -> list[str]:
    text = sys.stdin.read() if source == "-" else open(source).read()
    return [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "!"))
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", required=True)
    ap.add_argument("--file", required=True, help="file of config lines, or - for stdin")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    args = ap.parse_args()

    lines = read_lines(args.file)
    if not lines:
        print("!! nothing to send", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"would send {len(lines)} line(s) to {args.device}:")
        for line in lines:
            print(f"  {line}")
        return 0

    try:
        result = tasks.put_config(args.device, lines)
    except Exception as exc:
        print(f"!! {args.device}: {exc}", file=sys.stderr)
        return 1

    print(f"sent {result['lines_sent']} line(s) to {args.device}")
    print(f"previous config archived at {result['config_archived_to']}")
    print(result["output"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
