"""Assurance — does the device match what we expect, and what changed.

One engine, every platform. The pipeline is the same whatever the vendor:

    Netmiko   gets the text off the device
    TextFSM   parses tabular show output      (automation/textfsm/)
    TTP       parses hierarchical config      (automation/ttp/)
    normalise flattens vendor differences     (normalise.py)
    rules.yml declares what "healthy" means
    DeepDiff  compares two snapshots

Same guard as everywhere else: a check that collects nothing FAILS. An
assurance layer that silently stops assuring anything is worse than none.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from deepdiff import DeepDiff

from automation.assurance import normalise

RULES_FILE = Path(__file__).resolve().parent / "rules.yml"


def load_rules() -> list[dict]:
    with open(RULES_FILE) as fh:
        return (yaml.safe_load(fh) or {}).get("rules", [])


# --- individual checks -----------------------------------------------------

def _check_admin_up_means_oper_up(interfaces: list[dict], rule: dict) -> list[dict]:
    ignore = re.compile(rule["ignore"]) if rule.get("ignore") else None
    failures = []
    for iface in interfaces:
        if ignore and ignore.match(iface["interface"]):
            continue
        if iface["admin_up"] and not iface["oper_up"]:
            failures.append(
                {"interface": iface["interface"], "detail": "admin up but operationally down"}
            )
    return failures


def _check_min_interfaces(interfaces: list[dict], rule: dict) -> list[dict]:
    minimum = rule.get("minimum", 1)
    if len(interfaces) < minimum:
        return [{"interface": "-", "detail": f"only {len(interfaces)} interface(s), expected >= {minimum}"}]
    return []


CHECKS = {
    "admin_up_means_oper_up": _check_admin_up_means_oper_up,
    "min_interfaces": _check_min_interfaces,
}


# --- public API ------------------------------------------------------------

def run_rules(platform: str, rows: list[dict]) -> dict:
    """Run every applicable rule against a device's parsed state."""
    interfaces = normalise.normalise_interfaces(platform, rows)

    results, failed = [], 0
    for rule in load_rules():
        applies = rule.get("applies_to", "all")
        if applies != "all" and platform not in applies:
            continue

        check = CHECKS.get(rule["check"])
        if not check:
            results.append({
                "rule": rule["name"], "status": "error",
                "detail": f"unknown check '{rule['check']}' — add it to CHECKS in engine.py",
            })
            failed += 1
            continue

        failures = check(interfaces, rule)
        if failures:
            failed += 1
        results.append({
            "rule": rule["name"],
            "severity": rule.get("severity", "error"),
            "status": "fail" if failures else "pass",
            "description": rule.get("description", "").strip(),
            "failures": failures,
        })

    return {
        "platform": platform,
        "interfaces_checked": len(interfaces),
        "rules_run": len(results),
        "rules_failed": failed,
        "passed": failed == 0,
        "results": results,
    }


def snapshot(platform: str, rows: list[dict]) -> dict:
    """A comparable point-in-time view, keyed by interface.

    Deliberately drops the raw parsed row: counters and timers change every
    poll, so including them would make every diff noisy and useless.
    """
    interfaces = normalise.normalise_interfaces(platform, rows)
    return {
        i["interface"]: {"admin_up": i["admin_up"], "oper_up": i["oper_up"]}
        for i in interfaces
    }


def compare(before: dict, after: dict) -> dict:
    """What changed between two snapshots.

    DeepDiff rather than a hand-rolled comparison: it reports added, removed
    and changed values with paths, which is what makes a post-change check
    reviewable instead of a boolean.
    """
    if not before or not after:
        # An empty side would diff "clean" against anything — refuse rather
        # than report a change-free result that means nothing.
        raise ValueError("cannot compare: one of the snapshots is empty")

    diff = DeepDiff(before, after, ignore_order=True, verbose_level=2)
    changed = diff.to_dict()

    summary = []
    for path, change in (changed.get("values_changed") or {}).items():
        summary.append({
            "what": path.replace("root", "").strip("[]").replace("']['", " "),
            "from": change.get("old_value"),
            "to": change.get("new_value"),
        })
    for path in (changed.get("dictionary_item_added") or []):
        summary.append({"what": str(path), "from": None, "to": "added"})
    for path in (changed.get("dictionary_item_removed") or []):
        summary.append({"what": str(path), "from": "present", "to": "removed"})

    return {
        "changed": bool(summary),
        "change_count": len(summary),
        "changes": summary,
        "raw": {k: str(v) for k, v in changed.items()},
    }
