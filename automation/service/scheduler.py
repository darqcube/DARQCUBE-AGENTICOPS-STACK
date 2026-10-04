"""Scheduled assurance: run the rules on every device and expose the results.

Off by default (ASSURANCE_INTERVAL_MINUTES=0). It opens SSH — and, where
platforms.yml declares pyATS, unicon — sessions to every device on every run,
so a deployment turns it on deliberately. On demand (`make check`, the API,
mcp-assurance) works the same either way.

One run = every device in the inventory, AUTOMATION_CONCURRENCY at a time,
each through tasks.run_assurance — the same function, locks and parsers the
API uses, so a scheduled result and an on-demand one can never disagree.

Results are exposed at /metrics for Prometheus:

    assurance_rule_state{device, rule, severity, source, state} 1
        one series per device and rule; state is pass | fail | error | skipped
    assurance_device_last_run_timestamp_seconds{device}
    assurance_device_run_ok{device}          1 = the run completed
    assurance_device_run_duration_seconds{device}
    assurance_interval_seconds               the configured interval

Labels are bounded: devices x rules. Rule detail (which interface failed)
stays in the JSON at /assurance/latest, never in a label.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

INTERVAL_MINUTES = float(os.environ.get("ASSURANCE_INTERVAL_MINUTES", "0") or 0)
CONCURRENCY = int(os.environ.get("AUTOMATION_CONCURRENCY", "8"))

_lock = threading.Lock()
_results: dict[str, dict] = {}       # device -> {"ts", "ok", "duration", "rules": [...], "error"}
_started = False


def _run_device(device: str) -> None:
    from automation.nornir import tasks

    started = time.time()
    try:
        result = tasks.run_assurance(device)
        entry = {"ok": True, "rules": result.get("results", []), "error": None}
    except Exception as exc:  # one device's failure must not stop the run
        entry = {"ok": False, "rules": [], "error": str(exc)[:300]}
    entry.update(ts=time.time(), duration=time.time() - started)
    with _lock:
        _results[device] = entry


def run_once() -> int:
    """Assure every device once. Returns how many were attempted."""
    from automation.nornir import tasks

    devices = [d["name"] for d in tasks.list_devices()]
    with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as pool:
        list(pool.map(_run_device, devices))
    with _lock:   # a device removed from Infrahub stops being reported
        for gone in set(_results) - set(devices):
            _results.pop(gone, None)
    return len(devices)


def _loop() -> None:
    interval = INTERVAL_MINUTES * 60
    while True:
        started = time.time()
        try:
            n = run_once()
            print(f"assurance: {n} device(s) in {time.time() - started:.0f}s", flush=True)
        except Exception as exc:
            print(f"assurance: run failed: {exc}", flush=True)
        time.sleep(max(60.0, interval - (time.time() - started)))


def start() -> bool:
    """Start the background loop once, if enabled. Returns whether it runs."""
    global _started
    if INTERVAL_MINUTES <= 0 or _started:
        return _started
    _started = True
    threading.Thread(target=_loop, name="assurance-scheduler", daemon=True).start()
    return True


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metrics() -> str:
    """Prometheus text exposition of the latest results."""
    lines = [
        "# HELP assurance_interval_seconds Configured interval between scheduled assurance runs (0 = off).",
        "# TYPE assurance_interval_seconds gauge",
        f"assurance_interval_seconds {INTERVAL_MINUTES * 60:g}",
        "# HELP assurance_rule_state 1 for the current state of each rule on each device.",
        "# TYPE assurance_rule_state gauge",
    ]
    with _lock:
        snapshot = dict(_results)
    for device, entry in sorted(snapshot.items()):
        for r in entry["rules"]:
            labels = (f'device="{_esc(device)}",rule="{_esc(r.get("rule"))}",severity="{_esc(r.get("severity", ""))}",'
                      f'source="{_esc(r.get("source", ""))}",state="{_esc(r.get("status", "unknown"))}"')
            lines.append(f"assurance_rule_state{{{labels}}} 1")
    for name, help_, key in (
        ("assurance_device_last_run_timestamp_seconds", "When the device was last assured.", "ts"),
        ("assurance_device_run_ok", "1 if the last run completed, 0 if it could not run.", "ok"),
        ("assurance_device_run_duration_seconds", "How long the last run took.", "duration"),
    ):
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        for device, entry in sorted(snapshot.items()):
            lines.append(f'{name}{{device="{_esc(device)}"}} {float(entry[key]):g}')
    return "\n".join(lines) + "\n"


def latest() -> dict:
    """The latest results, with rule detail, for people and tools."""
    with _lock:
        return {"interval_minutes": INTERVAL_MINUTES, "devices": dict(sorted(_results.items()))}
