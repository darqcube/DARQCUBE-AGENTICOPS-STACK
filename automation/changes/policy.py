"""The rules a device change must pass. Pure functions: no I/O, no clock reads.

Who may act comes from RIGHTS, roles in the caller's token that the identity
provider gives to groups — never from a person's name. Four rights:

  propose      stage a change; withdraw it before it is approved
  approve      approve or reject a change (only through the approvals path,
               from a fresh sign-in; see mcp/servers/netmiko.py)
  commit       commit an approved change the caller asked for or approved
  commit_any   also commit a colleague's approved change (a shift handover)

The commit right and the change's own record work together: the right says
who may push at all, the record says which change.

A caller authenticated with the shared MCP token is a machine, not a person:
it may not take part in a change. Maker-checker needs two people to be named.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

WAITING, APPROVED, COMMITTING, COMMITTED = "waiting", "approved", "committing", "committed"
REJECTED, WITHDRAWN, EXPIRED, FAILED = "rejected", "withdrawn", "expired", "failed"
CLOSED = (REJECTED, WITHDRAWN, EXPIRED, FAILED)


class Refused(Exception):
    """The action breaks a rule. The message says which, for a person to read."""


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    approve_within: int = 120 * 60      # seconds a change waits for a decision
    commit_within: int = 15 * 60        # seconds from approval to commit
    rollback_within: int = 60 * 60      # seconds after the push the rollback stays allowed
    fresh_signin: int = 300             # an approval's sign-in may be at most this old
    four_eyes: bool = False             # the approver must not be the person who asked
    max_lines: int = 200
    roles: dict[str, str] = field(default_factory=lambda: {
        "propose": "darqcube-propose", "approve": "darqcube-approve",
        "commit": "darqcube-commit", "commit_any": "darqcube-commit-any",
    })

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            approve_within=_int("CHANGE_APPROVE_WITHIN_MINUTES", 120) * 60,
            commit_within=_int("CHANGE_COMMIT_WITHIN_MINUTES", 15) * 60,
            rollback_within=_int("CHANGE_ROLLBACK_WITHIN_MINUTES", 60) * 60,
            fresh_signin=_int("CHANGE_FRESH_SIGNIN_SECONDS", 300),
            four_eyes=_bool("CHANGE_FOUR_EYES"),
            max_lines=_int("MAX_CONFIG_LINES", 200),
            roles={
                "propose": os.environ.get("CHANGE_PROPOSE_ROLE", "").strip() or "darqcube-propose",
                "approve": os.environ.get("CHANGE_APPROVE_ROLE", "").strip() or "darqcube-approve",
                "commit": os.environ.get("CHANGE_COMMIT_ROLE", "").strip() or "darqcube-commit",
                "commit_any": os.environ.get("CHANGE_COMMIT_ANY_ROLE", "").strip() or "darqcube-commit-any",
            },
        )


def fingerprint(device: str, lines: list[str], rollback: list[str]) -> str:
    """SHA-256 of exactly what was approved: the device, the lines in order, the rollback."""
    canonical = json.dumps({"device": device, "lines": lines, "rollback": rollback},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _person(actor: dict) -> None:
    if actor.get("via") != "oidc" or not actor.get("sub"):
        raise Refused("a change needs a named person: the shared MCP token cannot take part")


def _has(actor: dict, settings: Settings, right: str) -> bool:
    return settings.roles[right] in set(actor.get("roles") or ())


def check_stage(actor: dict, device: str, lines: list[str], rollback: list[str], settings: Settings) -> None:
    _person(actor)
    if not _has(actor, settings, "propose"):
        raise Refused(f"{actor.get('user')} lacks the propose right ({settings.roles['propose']})")
    if not lines:
        raise Refused("no configuration lines")
    if len(lines) > settings.max_lines or len(rollback) > settings.max_lines:
        raise Refused(f"more than {settings.max_lines} lines (MAX_CONFIG_LINES)")
    for line in [*lines, *rollback]:
        # One element is one command. A newline inside it would carry a second,
        # unchecked command past the protected-line rules.
        if len(line) > 512 or any(ord(ch) < 32 and ch != "\t" for ch in line):
            raise Refused(f"line {line[:40]!r}…: one command per line, no control characters, at most 512 characters")


def expire(change: dict, now: float) -> str | None:
    """The state a change should move to because time passed, or None."""
    if change["state"] == WAITING and now > change["approve_by"]:
        return EXPIRED
    if change["state"] == APPROVED and now > change["commit_by"]:
        return EXPIRED
    return None


def _open(change: dict, now: float, wanted: str) -> None:
    lapsed = expire(change, now)
    if lapsed:
        raise Refused(f"change {change['id']} expired")
    if change["state"] != wanted:
        raise Refused(f"change {change['id']} is {change['state']}, not {wanted}")


def check_decide(actor: dict, change: dict, now: float, settings: Settings) -> None:
    """Approve or reject. The caller is the approvals path's verified person."""
    _person(actor)
    _open(change, now, WAITING)
    if not _has(actor, settings, "approve"):
        raise Refused(f"{actor.get('user')} lacks the approve right ({settings.roles['approve']})")
    auth_time = actor.get("auth_time")
    if not isinstance(auth_time, (int, float)) or now - auth_time > settings.fresh_signin:
        raise Refused(f"an approval needs a fresh sign-in (within {settings.fresh_signin} s)")
    if settings.four_eyes and actor["sub"] == change["requester_sub"]:
        raise Refused("four eyes: the person who asked cannot approve their own change")


def check_withdraw(actor: dict, change: dict, now: float) -> None:
    _person(actor)
    _open(change, now, WAITING)
    if actor["sub"] != change["requester_sub"]:
        raise Refused("only the person who asked can withdraw a change")


def _may_commit(actor: dict, change: dict, settings: Settings) -> None:
    if _has(actor, settings, "commit_any"):
        return
    if not _has(actor, settings, "commit"):
        raise Refused(f"{actor.get('user')} lacks the commit right ({settings.roles['commit']})")
    if actor["sub"] not in (change["requester_sub"], change.get("approver_sub")):
        raise Refused("only the person who asked or the approver commits this change "
                      f"(or the {settings.roles['commit_any']} right)")


def check_commit(actor: dict, change: dict, now: float, settings: Settings) -> None:
    _person(actor)
    _open(change, now, APPROVED)
    _may_commit(actor, change, settings)
    if fingerprint(change["device"], change["lines"], change["rollback"]) != change["fingerprint"]:
        raise Refused("the change no longer matches what was approved (fingerprint differs)")


def check_rollback(actor: dict, change: dict, now: float, settings: Settings) -> None:
    _person(actor)
    if change["state"] != COMMITTED:
        raise Refused(f"change {change['id']} is {change['state']}: only a pushed change rolls back")
    if not change["rollback"]:
        raise Refused(f"change {change['id']} declared no rollback")
    if change.get("rollback_used_at"):
        raise Refused(f"the rollback of change {change['id']} was already used")
    if now > change["committed_at"] + settings.rollback_within:
        raise Refused(f"the rollback of change {change['id']} expired")
    _may_commit(actor, change, settings)
    if fingerprint(change["device"], change["lines"], change["rollback"]) != change["fingerprint"]:
        raise Refused("the change no longer matches what was approved (fingerprint differs)")
