"""Runs INSIDE darqcube/automation:local, driven by test_changes_api.py.

The automation API's /changes endpoints with the real store and rules; only
the inventory lookup and the device push are replaced. Pinned:

- WRITE_MODE=approved refuses the raw one-step push (403, names the way to go)
- stage -> approve -> commit pushes exactly the staged lines, once
- a refusal is a 403 with the rule's reason, and nothing is pushed
- a protected line is refused at staging; an unknown device is 404
- a push that fails consumes the change and is recorded, never retried
- every action, allowed or refused, is in /changes/events

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace

os.environ.update({"WRITE_MODE": "approved", "CHANGES_DB": os.path.join(tempfile.mkdtemp(), "changes.db")})
sys.path.insert(0, "/app")

from fastapi.testclient import TestClient  # noqa: E402

from automation.nornir import tasks  # noqa: E402
from automation.service import main  # noqa: E402

INVENTORY = {"router1", "router2"}
pushed: list[tuple[str, list[str]]] = []
fail_next = {"on": False}
results: dict[str, list] = {}


def fake_get_nornir(device=None, fresh=False):
    if device not in INVENTORY:
        raise tasks.DeviceError(f"device '{device}' is not in Infrahub")
    return SimpleNamespace(inventory=SimpleNamespace(hosts={device: object()}))


def fake_put_config(device, lines):
    if fail_next["on"]:
        fail_next["on"] = False
        raise tasks.DeviceError(f"{device}: unreachable")
    pushed.append((device, list(lines)))
    return {"device": device, "lines_sent": len(lines), "config_archived_to": f"configs/{device}.cfg", "output": ""}


tasks.get_nornir = fake_get_nornir
tasks.put_config = fake_put_config


def actor(user, *roles, auth_time=None):
    return {"user": user, "sub": f"sub-{user}", "roles": list(roles), "via": "oidc", "agent": "agent-x",
            "auth_time": auth_time}


ASKER = actor("engineer", "darqcube-propose", "darqcube-commit")
LINES = ["interface Ethernet0/1", " no shutdown"]


def approver():
    return actor("manager", "darqcube-approve", "darqcube-commit", auth_time=time.time() - 5)


def check(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


with TestClient(main.app) as c:
    def stage(lines=LINES, device="router1"):
        return c.post("/changes", json={"actor": ASKER, "device": device, "lines": lines,
                                        "rollback": ["interface Ethernet0/1", " shutdown"], "source": "chat"})

    def raw_push_refused():
        r = c.post("/device/router1/config", json={"lines": ["x"]})
        return r.status_code == 403 and "approve-changes" in r.text and not pushed, (r.status_code, r.text[:200])
    check("raw_push_is_refused_in_approved_mode", raw_push_refused)

    def full_path():
        pushed.clear()
        change = stage().json()
        a = c.post(f"/changes/{change['id']}/approve", json={"actor": approver()})
        m = c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        ok = (a.status_code == 200 and m.status_code == 200 and m.json()["state"] == "committed"
              and pushed == [("router1", LINES)])
        return ok, (a.status_code, m.status_code, pushed)
    check("stage_approve_commit_pushes_exactly_the_staged_lines", full_path)

    def commit_unapproved():
        pushed.clear()
        change = stage().json()
        r = c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        return r.status_code == 403 and "not approved" in r.text and not pushed, (r.status_code, r.text[:200])
    check("commit_before_approval_is_403_and_pushes_nothing", commit_unapproved)

    def second_commit():
        pushed.clear()
        change = stage().json()
        c.post(f"/changes/{change['id']}/approve", json={"actor": approver()})
        c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        r = c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        return r.status_code == 403 and len(pushed) == 1, (r.status_code, len(pushed))
    check("an_approval_pushes_once", second_commit)

    def stale_approval():
        change = stage().json()
        old = actor("manager", "darqcube-approve", auth_time=time.time() - 3600)
        r = c.post(f"/changes/{change['id']}/approve", json={"actor": old})
        return r.status_code == 403 and "fresh sign-in" in r.text, (r.status_code, r.text[:200])
    check("an_approval_without_a_fresh_sign_in_is_403", stale_approval)

    def protected():
        r = stage(lines=["reload"])
        return r.status_code == 403 and "reload" in r.text, (r.status_code, r.text[:200])
    check("a_protected_line_is_refused_at_staging", protected)

    def unknown_device():
        r = stage(device="nowhere")
        return r.status_code == 404, r.status_code
    check("an_unknown_device_is_404", unknown_device)

    def failed_push():
        pushed.clear()
        change = stage().json()
        c.post(f"/changes/{change['id']}/approve", json={"actor": approver()})
        fail_next["on"] = True
        r = c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        again = c.post(f"/changes/{change['id']}/commit", json={"actor": ASKER})
        state = c.get(f"/changes/{change['id']}").json()["state"]
        return r.status_code == 502 and state == "failed" and again.status_code == 403 and not pushed, \
            (r.status_code, state, again.status_code)
    check("a_failed_push_is_recorded_and_not_retried", failed_push)

    def events():
        r = c.get("/changes/events?limit=500").json()["events"]
        outcomes = {(e["action"], e["outcome"]) for e in r}
        ok = {("stage", "ok"), ("approve", "ok"), ("commit", "ok"), ("commit", "refused"),
              ("stage", "refused"), ("approve", "refused"), ("commit", "failed")} <= outcomes
        return ok, sorted(outcomes)
    check("every_action_allowed_or_refused_is_an_event", events)

    def listing():
        r = c.get("/changes?state=committed").json()["changes"]
        return r and all(x["state"] == "committed" for x in r), len(r)
    check("changes_list_by_state", listing)

print(json.dumps(results))
