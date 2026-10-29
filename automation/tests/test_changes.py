"""Approved changes — the rules, the protected lines and the store. Offline, seconds.

One test per guarantee in docs/how-to/approve-changes.md ("What it refuses"):
a change reaches a device only when a person with the approve right approved
exactly these lines, from a fresh sign-in, and it is committed once, in time,
by the person who asked, the approver, or someone whose group commits any
approved change.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from automation.changes import lines as line_rules  # noqa: E402
from automation.changes import policy  # noqa: E402
from automation.changes.policy import Refused, Settings  # noqa: E402
from automation.changes.store import NotFound, Store  # noqa: E402

LINES = ["interface Ethernet0/1", " no shutdown"]
ROLLBACK = ["interface Ethernet0/1", " shutdown"]
T0 = 1_000_000.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def person(user, *rights, auth_time=None, agent="agent-change-executor"):
    s = Settings()
    roles = [s.roles[r] for r in rights]
    return {"user": user, "sub": f"sub-{user}", "roles": roles, "via": "oidc", "agent": agent,
            "auth_time": auth_time}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "changes.db", Settings(), clock=clock)


ASKER = person("engineer", "propose", "commit")
APPROVER_RIGHTS = ("propose", "approve", "commit")


def approver(clock, user="manager", **kw):
    return person(user, *APPROVER_RIGHTS, auth_time=clock.now - 10, **kw)


def staged(store):
    return store.stage(ASKER, "router1", LINES, ROLLBACK, source="chat")


def approved(store, clock):
    change = staged(store)
    store.decide(change["id"], approver(clock), approve=True)
    return change


# --- the happy path ------------------------------------------------------------

def test_stage_approve_commit(store, clock):
    change = staged(store)
    assert change["state"] == "waiting" and change["requester"] == "engineer"
    assert change["requester_agent"] == "agent-change-executor"
    assert change["fingerprint"] == policy.fingerprint("router1", LINES, ROLLBACK)
    store.decide(change["id"], approver(clock), approve=True)
    claimed = store.begin_commit(change["id"], ASKER)
    assert claimed["state"] == "committing"
    done = store.finish(change["id"], result={"lines_sent": 2})
    assert done["state"] == "committed" and done["committer"] == "engineer"
    actions = [(e["action"], e["outcome"]) for e in store.events(change_id=change["id"])][::-1]
    assert actions == [("stage", "ok"), ("approve", "ok"), ("commit", "started"), ("commit", "ok")]


# --- what it refuses -----------------------------------------------------------

def test_commit_without_approval_is_refused(store):
    change = staged(store)
    with pytest.raises(Refused, match="waiting, not approved"):
        store.begin_commit(change["id"], ASKER)


def test_the_shared_token_cannot_take_part(store):
    machine = {"user": "token", "sub": "token", "roles": ["*"], "via": "token"}
    with pytest.raises(Refused, match="named person"):
        store.stage(machine, "router1", LINES, ROLLBACK)


def test_propose_right_is_needed_to_stage(store):
    with pytest.raises(Refused, match="propose right"):
        store.stage(person("viewer"), "router1", LINES, ROLLBACK)


def test_approve_right_is_needed(store, clock):
    change = staged(store)
    engineer = person("engineer", "propose", "commit", auth_time=clock.now)
    with pytest.raises(Refused, match="approve right"):
        store.decide(change["id"], engineer, approve=True)


def test_an_approval_needs_a_fresh_sign_in(store, clock):
    change = staged(store)
    stale = person("manager", *APPROVER_RIGHTS, auth_time=clock.now - 3600)
    with pytest.raises(Refused, match="fresh sign-in"):
        store.decide(change["id"], stale, approve=True)
    with pytest.raises(Refused, match="fresh sign-in"):
        store.decide(change["id"], person("manager", *APPROVER_RIGHTS), approve=True)


def test_four_eyes(tmp_path, clock):
    strict = Store(tmp_path / "c.db", Settings(four_eyes=True), clock=clock)
    asker = person("manager", *APPROVER_RIGHTS)
    change = strict.stage(asker, "router1", LINES, ROLLBACK)
    with pytest.raises(Refused, match="four eyes"):
        strict.decide(change["id"], approver(clock), approve=True)
    strict.decide(change["id"], approver(clock, user="other-manager"), approve=True)


def test_an_approval_is_used_once(store, clock):
    change = approved(store, clock)
    store.begin_commit(change["id"], ASKER)
    store.finish(change["id"], result={})
    with pytest.raises(Refused, match="committed, not approved"):
        store.begin_commit(change["id"], ASKER)


def test_two_commits_at_once_only_one_starts(store, clock):
    change = approved(store, clock)
    store.begin_commit(change["id"], ASKER)
    with pytest.raises(Refused, match="committing, not approved"):
        store.begin_commit(change["id"], ASKER)


def test_waiting_expires(store, clock):
    change = staged(store)
    clock.now += 121 * 60
    assert store.get(change["id"])["state"] == "expired"
    with pytest.raises(Refused, match="expired"):
        store.decide(change["id"], approver(clock), approve=True)


def test_an_approval_expires_if_not_committed(store, clock):
    change = approved(store, clock)
    clock.now += 16 * 60
    with pytest.raises(Refused, match="expired"):
        store.begin_commit(change["id"], ASKER)


def test_lines_changed_after_approval_are_refused(store, clock):
    change = approved(store, clock)
    tampered = store.get(change["id"])
    tampered["lines"] = ["interface Ethernet0/1", " shutdown"]
    store._save(tampered)
    with pytest.raises(Refused, match="fingerprint differs"):
        store.begin_commit(change["id"], ASKER)


def test_own_changes_scope_cannot_commit_a_colleagues_change(store, clock):
    change = approved(store, clock)
    with pytest.raises(Refused, match="only the person who asked or the approver"):
        store.begin_commit(change["id"], person("colleague", "propose", "commit"))


def test_the_approver_can_commit(store, clock):
    change = approved(store, clock)
    assert store.begin_commit(change["id"], approver(clock))["committer"] == "manager"


def test_commit_any_scope_can_commit_a_colleagues_change(store, clock):
    change = approved(store, clock)
    lead = person("shift-lead", "propose", "commit_any")
    assert store.begin_commit(change["id"], lead)["committer"] == "shift-lead"


def test_the_commit_right_is_needed_even_for_your_own_change(store, clock):
    asker = person("proposer", "propose")
    change = store.stage(asker, "router1", LINES, ROLLBACK)
    store.decide(change["id"], approver(clock), approve=True)
    with pytest.raises(Refused, match="commit right"):
        store.begin_commit(change["id"], asker)


def test_rejected_and_withdrawn_changes_are_closed(store, clock):
    change = staged(store)
    store.decide(change["id"], approver(clock), approve=False, reason="not now")
    with pytest.raises(Refused, match="rejected"):
        store.begin_commit(change["id"], ASKER)
    other = staged(store)
    with pytest.raises(Refused, match="only the person who asked"):
        store.withdraw(other["id"], approver(clock))
    assert store.withdraw(other["id"], ASKER)["state"] == "withdrawn"


def test_rollback_once_within_the_hour(store, clock):
    change = approved(store, clock)
    store.begin_commit(change["id"], ASKER)
    store.finish(change["id"], result={})
    claimed = store.begin_commit(change["id"], ASKER, rollback=True)
    assert claimed["rollback_used_at"]
    with pytest.raises(Refused, match="already used"):
        store.begin_commit(change["id"], ASKER, rollback=True)


def test_rollback_expires(store, clock):
    change = approved(store, clock)
    store.begin_commit(change["id"], ASKER)
    store.finish(change["id"], result={})
    clock.now += 61 * 60
    with pytest.raises(Refused, match="rollback .* expired"):
        store.begin_commit(change["id"], ASKER, rollback=True)


def test_a_failed_push_consumes_the_approval(store, clock):
    change = approved(store, clock)
    store.begin_commit(change["id"], ASKER)
    assert store.finish(change["id"], error="device unreachable")["state"] == "failed"
    with pytest.raises(Refused, match="failed, not approved"):
        store.begin_commit(change["id"], ASKER)


def test_an_interrupted_push_is_marked_failed_at_start(tmp_path, clock):
    first = Store(tmp_path / "c.db", Settings(), clock=clock)
    change = first.stage(ASKER, "router1", LINES, ROLLBACK)
    first.decide(change["id"], approver(clock), approve=True)
    first.begin_commit(change["id"], ASKER)
    again = Store(tmp_path / "c.db", Settings(), clock=clock)
    assert again.get(change["id"])["state"] == "failed"
    assert "check the device" in again.get(change["id"])["error"]


def test_every_refusal_is_recorded_with_the_person_and_agent(store, clock):
    change = staged(store)
    with pytest.raises(Refused):
        store.begin_commit(change["id"], ASKER)
    refused = [e for e in store.events() if e["outcome"] == "refused"]
    assert refused and refused[0]["user"] == "engineer" and refused[0]["agent"] == "agent-change-executor"


def test_unknown_change(store):
    with pytest.raises(NotFound):
        store.get("0000-0000")


@pytest.mark.parametrize("smuggled", ["description x\nreload", "description x\rreload", "x" * 600])
def test_one_command_per_line(store, smuggled):
    with pytest.raises(Refused, match="one command per line"):
        store.stage(ASKER, "router1", ["interface Ethernet0/1", smuggled], [])


def test_too_many_lines(tmp_path, clock):
    small = Store(tmp_path / "c.db", Settings(max_lines=1), clock=clock)
    with pytest.raises(Refused, match="more than 1 lines"):
        small.stage(ASKER, "router1", LINES, ROLLBACK)


# --- protected lines -----------------------------------------------------------

@pytest.mark.parametrize("line, rule", [
    ("reload", "reload"), ("reload in 5", "reload"), ("/system reboot", "reload"),
    ("write erase", "erase-configuration"), ("erase startup-config", "erase-configuration"),
    ("reset saved-configuration", "erase-configuration"),
    ("no router bgp 65000", "remove-routing-process"), ("undo bgp", "remove-routing-process"),
    ("username admin privilege 15 secret x", "local-users-and-aaa"), ("no aaa new-model", "local-users-and-aaa"),
    ("local-user admin", "local-users-and-aaa"), ("/user add name=x", "local-users-and-aaa"),
])
def test_protected_lines_are_refused(line, rule):
    found = line_rules.refusals([line])
    assert found and rule in found[0]


@pytest.mark.parametrize("lines", [
    ["interface Ethernet0/1", " no shutdown"], ["router bgp 65000", " neighbor 192.0.2.1 shutdown"],
    ["interface Ethernet0/1", " description uplink to hq"], ["ip route 0.0.0.0 0.0.0.0 192.0.2.1"],
])
def test_ordinary_changes_pass(lines):
    assert line_rules.refusals(lines) == []


def test_a_protected_line_is_refused_at_staging_and_named(store):
    with pytest.raises(Refused, match=r"line 2 .*no router bgp 65000.*remove-routing-process"):
        store.stage(ASKER, "router1", ["router bgp 65000", "no router bgp 65000"], [])


def test_the_rollback_is_checked_too(store):
    with pytest.raises(Refused, match="rollback line 1"):
        store.stage(ASKER, "router1", LINES, ["reload"])


def test_management_interface_cannot_be_shut(monkeypatch):
    monkeypatch.setenv("CHANGE_MANAGEMENT_INTERFACES", "GigabitEthernet0/0, mgmt0")
    assert line_rules.refusals(["interface GigabitEthernet0/0", " shutdown"])
    assert line_rules.refusals(["interface mgmt0", "no ip address"]), "unindented lines stay inside the block"
    assert line_rules.refusals(["interface GigabitEthernet0/0", " description x", " no ip address"])
    assert not line_rules.refusals(["interface GigabitEthernet0/1", " shutdown"])
    assert not line_rules.refusals(["interface GigabitEthernet0/0", "interface Ethernet0/2", " shutdown"])
    assert not line_rules.refusals(["interface GigabitEthernet0/0", "!", "shutdown"]), "a block ends at !"


def test_a_malformed_rule_file_fails_loudly(tmp_path):
    bad = tmp_path / "rules.yml"
    bad.write_text("rules:\n  - name: x\n    match: '('\n")
    with pytest.raises(line_rules.LineRuleError):
        line_rules.refusals(["anything"], path=str(bad))
