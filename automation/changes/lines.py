"""Protected lines: refused when a change is staged, before anyone is asked.

The rules are data (protected.yml). A refusal names the line, the rule and
why, so the person who asked can see exactly what to change.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_FILE = Path(__file__).with_name("protected.yml")

# A line that starts a new top-level context ends the current interface block.
# Lines in a change are often sent without indentation, so "not indented" alone
# cannot end a block: staying inside is the safe side (a false refusal is seen
# and fixed by a person; a missed one cuts a device off).
_TOP_LEVEL = re.compile(r"^(?:(?:interface|router|line|vlan|hostname|aaa|username|banner|logging|snmp-server|ntp|"
                        r"ip\s+route|end|exit|quit|return)\b|!)", re.IGNORECASE)
_INTERFACE = re.compile(r"^interface\s+(\S+)", re.IGNORECASE)


class LineRuleError(ValueError):
    """protected.yml is malformed. Raised at load, never silently ignored."""


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[list[tuple[str, str, re.Pattern]], re.Pattern | None]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    rules = []
    for rule in data.get("rules") or []:
        try:
            rules.append((rule["name"], rule.get("why", ""), re.compile(rule["match"], re.IGNORECASE)))
        except (KeyError, TypeError, re.error) as exc:
            raise LineRuleError(f"{path}: bad rule {rule!r}: {exc}") from exc
    inside = (data.get("management") or {}).get("inside_match")
    return rules, re.compile(inside, re.IGNORECASE) if inside else None


def _management_interfaces() -> set[str]:
    raw = os.environ.get("CHANGE_MANAGEMENT_INTERFACES", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def refusals(lines: list[str], path: str | None = None) -> list[str]:
    """Every protected line in `lines`, as one sentence each. Empty = allowed."""
    rules, inside = _load(path or os.environ.get("CHANGE_PROTECTED_FILE", "").strip() or str(DEFAULT_FILE))
    management = _management_interfaces()
    found, current = [], None
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        opened = _INTERFACE.match(line)
        if opened:
            current = opened.group(1).lower()
        elif not raw[:1].isspace() and _TOP_LEVEL.match(line):
            current = None
        for name, why, pattern in rules:
            if pattern.search(line):
                found.append(f"line {number} “{line}”: {name} ({why})")
        if current in management and inside and not opened and inside.search(line):
            found.append(f"line {number} “{line}”: management interface {current} (would cut the device off)")
    return found
