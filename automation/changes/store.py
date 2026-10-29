"""The changes, and every decision on them, in one SQLite file.

SQLite because the automation API runs ONE worker (see scheduler.py) and a
change is a few rows: no server to run, and the file lives on a named volume so
a container rebuild keeps the history. Every state transition happens under one
lock, so two commits of the same change cannot both start.

Every action is an event, allowed or refused, with the person and the agent:
that list is the audit an approver reads (History).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import lines as line_rules
from . import policy
from .policy import APPROVED, COMMITTED, COMMITTING, EXPIRED, FAILED, REJECTED, WAITING, WITHDRAWN, Refused

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, state TEXT NOT NULL, staged_at REAL NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS changes_state ON changes(state, staged_at);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, change_id TEXT,
  action TEXT NOT NULL, outcome TEXT NOT NULL, user TEXT, agent TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
"""


class NotFound(LookupError):
    pass


def _who(actor: dict) -> tuple[str, str | None]:
    return str(actor.get("user") or "?"), actor.get("agent")


class Store:
    def __init__(self, path: str | Path, settings: policy.Settings | None = None, clock=time.time):
        self.settings = settings or policy.Settings.from_env()
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        with self._lock:
            # A push interrupted by a restart: the device may or may not have
            # it. Never retried by itself; a person looks at the device.
            for change in self._select("state = ?", (COMMITTING,)):
                self._finish(change, FAILED, error="interrupted by a restart: check the device before retrying")

    # --- reading ------------------------------------------------------------

    def get(self, change_id: str) -> dict:
        with self._lock:
            return self._get(change_id)

    def list(self, state: str | None = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            self._expire_all()
            if state:
                return self._select("state = ? ORDER BY staged_at DESC LIMIT ?", (state, limit))
            return self._select("1 ORDER BY staged_at DESC LIMIT ?", (limit,))

    def events(self, limit: int = 200, change_id: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        where, args = ("WHERE change_id = ?", (change_id, limit)) if change_id else ("", (limit,))
        rows = self._db.execute(
            f"SELECT t, change_id, action, outcome, user, agent, detail FROM events {where} ORDER BY id DESC LIMIT ?",
            args).fetchall()
        keys = ("t", "change_id", "action", "outcome", "user", "agent", "detail")
        return [dict(zip(keys, row)) for row in rows]

    # --- acting -------------------------------------------------------------

    def stage(self, actor: dict, device: str, lines: list[str], rollback: list[str], source: str = "") -> dict:
        now = self.clock()
        with self._lock:
            try:
                policy.check_stage(actor, device, lines, rollback, self.settings)
                protected = line_rules.refusals(lines) + [f"rollback {r}" for r in line_rules.refusals(rollback)]
                if protected:
                    raise Refused("protected lines: " + "; ".join(protected))
            except Refused as exc:
                self._event(None, "stage", "refused", actor, f"{device}: {exc}")
                raise
            change = {
                "id": self._new_id(), "device": device, "lines": lines, "rollback": rollback,
                "fingerprint": policy.fingerprint(device, lines, rollback), "state": WAITING,
                "source": source[:64], "requester": actor.get("user"), "requester_sub": actor["sub"],
                "requester_agent": actor.get("agent"), "staged_at": now,
                "approve_by": now + self.settings.approve_within,
            }
            self._db.execute("INSERT INTO changes (id, state, staged_at, data) VALUES (?, ?, ?, ?)",
                             (change["id"], WAITING, now, json.dumps(change)))
            self._event(change["id"], "stage", "ok", actor, f"{device}: {len(lines)} lines")
            return change

    def decide(self, change_id: str, actor: dict, approve: bool, reason: str = "") -> dict:
        action = "approve" if approve else "reject"
        now = self.clock()
        with self._lock:
            change = self._get(change_id)
            self._check(policy.check_decide, actor, change, now, action=action)
            user, agent = _who(actor)
            change.update(approver=user, approver_sub=actor["sub"], approver_agent=agent, decided_at=now)
            if approve:
                change.update(state=APPROVED, commit_by=now + self.settings.commit_within)
            else:
                change.update(state=REJECTED, reject_reason=reason[:300])
            self._save(change)
            self._event(change_id, action, "ok", actor, reason[:300])
            return change

    def withdraw(self, change_id: str, actor: dict) -> dict:
        now = self.clock()
        with self._lock:
            change = self._get(change_id)
            self._check(lambda a, c, n, s: policy.check_withdraw(a, c, n), actor, change, now, action="withdraw")
            change.update(state=WITHDRAWN, withdrawn_at=now)
            self._save(change)
            self._event(change_id, "withdraw", "ok", actor, "")
            return change

    def begin_commit(self, change_id: str, actor: dict, rollback: bool = False) -> dict:
        """Check and claim the change for one push. The caller pushes, then finish()."""
        action = "rollback" if rollback else "commit"
        now = self.clock()
        with self._lock:
            change = self._get(change_id)
            check = policy.check_rollback if rollback else policy.check_commit
            self._check(check, actor, change, now, action=action)
            user, agent = _who(actor)
            if rollback:
                change.update(rollback_used_at=now, rollback_by=user, rollback_agent=agent)
            else:
                change.update(state=COMMITTING, committer=user, committer_agent=agent, commit_started_at=now)
            self._save(change)
            self._event(change_id, action, "started", actor, "")
            return change

    def finish(self, change_id: str, result: dict | None = None, error: str = "", rollback: bool = False) -> dict:
        with self._lock:
            change = self._get(change_id)
            if rollback:
                change.update(rollback_result=result, rollback_error=error[:500])
                self._save(change)
                self._event(change_id, "rollback", "ok" if not error else "failed",
                            {"user": change.get("rollback_by"), "agent": change.get("rollback_agent")}, error[:500])
                return change
            return self._finish(change, FAILED if error else COMMITTED, result=result, error=error)

    # --- internals ----------------------------------------------------------

    def _check(self, check, actor: dict, change: dict, now: float, action: str) -> None:
        try:
            check(actor, change, now, self.settings)
        except Refused as exc:
            self._event(change["id"], action, "refused", actor, str(exc))
            raise

    def _finish(self, change: dict, state: str, result: dict | None = None, error: str = "") -> dict:
        now = self.clock()
        change.update(state=state, committed_at=now, result=result, error=error[:500])
        self._save(change)
        self._event(change["id"], "commit", "ok" if state == COMMITTED else "failed",
                    {"user": change.get("committer"), "agent": change.get("committer_agent")}, error[:500])
        return change

    def _new_id(self) -> str:
        while True:
            raw = secrets.token_hex(4)
            change_id = f"{raw[:4]}-{raw[4:]}"
            if not self._db.execute("SELECT 1 FROM changes WHERE id = ?", (change_id,)).fetchone():
                return change_id

    def _get(self, change_id: str) -> dict:
        row = self._db.execute("SELECT data FROM changes WHERE id = ?", (change_id,)).fetchone()
        if not row:
            raise NotFound(f"no change {change_id!r}")
        return self._expire(json.loads(row[0]))

    def _select(self, where: str, args: tuple) -> list[dict]:
        rows = self._db.execute(f"SELECT data FROM changes WHERE {where}", args).fetchall()
        return [self._expire(json.loads(row[0])) for row in rows]

    def _expire(self, change: dict) -> dict:
        lapsed = policy.expire(change, self.clock())
        if lapsed:
            was = change["state"]
            change.update(state=lapsed, expired_at=self.clock())
            self._save(change)
            self._event(change["id"], "expire", "ok", {"user": "stack"},
                        "no decision in time" if was == WAITING else "approved but not committed in time")
        return change

    def _expire_all(self) -> None:
        for (data,) in self._db.execute("SELECT data FROM changes WHERE state IN (?, ?)", (WAITING, APPROVED)).fetchall():
            self._expire(json.loads(data))

    def _save(self, change: dict) -> None:
        self._db.execute("UPDATE changes SET state = ?, data = ? WHERE id = ?",
                         (change["state"], json.dumps(change), change["id"]))

    def _event(self, change_id: str | None, action: str, outcome: str, actor: dict, detail: str) -> None:
        user, agent = _who(actor)
        self._db.execute("INSERT INTO events (t, change_id, action, outcome, user, agent, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (self.clock(), change_id, action, outcome, user, agent, detail))


# Exported for the API: states a person reads as "done".
CLOSED_STATES = (COMMITTED, REJECTED, WITHDRAWN, EXPIRED, FAILED)
