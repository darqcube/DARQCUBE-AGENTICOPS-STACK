"""Offline tests for render-hook.py — auto-render on Infrahub changes.

The hook rewrites collector configuration, so the checks that matter are the
ones that keep it from doing so for the wrong reason: signature, freshness,
relevance, and one render per burst rather than one per event.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SECRET = "s3cret"


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setenv("RENDER_HOOK_SECRET", SECRET)
    spec = importlib.util.spec_from_file_location("render_hook", ROOT / "source-of-truth/scripts/render-hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def signed(payload: dict, secret: str = SECRET, stamp: int | None = None, msg_id: str = "msg_1"):
    """Headers exactly as Infrahub builds them (compact JSON), body as httpx sends it."""
    stamp = stamp or int(time.time())
    compact = json.dumps(payload, separators=(",", ":"))
    sig = base64.b64encode(hmac.new(secret.encode(), f"{msg_id}.{stamp}.{compact}".encode(),
                                    hashlib.sha256).digest()).decode()
    headers = {"webhook-id": msg_id, "webhook-timestamp": str(stamp), "webhook-signature": f"v1,{sig}"}
    return headers, json.dumps(payload).encode()          # default separators, as on the wire


EVENT = {"event": "infrahub.node.updated", "data": {"kind": "NetworkDevice"}}


def test_a_request_signed_like_infrahub_is_accepted(hook):
    headers, body = signed(EVENT)
    assert hook.verify(headers, body) is None


def test_unsigned_forged_and_stale_requests_are_refused(hook):
    _, body = signed(EVENT)
    assert hook.verify({}, body) == "unsigned request"
    headers, body = signed(EVENT, secret="wrong")
    assert hook.verify(headers, body) == "bad signature"
    headers, body = signed(EVENT, stamp=int(time.time()) - 3600)
    assert hook.verify(headers, body).startswith("stale request")


def test_a_tampered_body_fails_the_signature(hook):
    headers, _ = signed(EVENT)
    tampered = json.dumps({**EVENT, "data": {"kind": "CoreAccount"}}).encode()
    assert hook.verify(headers, tampered) == "bad signature"


def test_one_of_several_signatures_may_match(hook):
    """Standard Webhooks allows space-separated signatures during key rotation."""
    headers, body = signed(EVENT)
    headers["webhook-signature"] = "v1,AAAA " + headers["webhook-signature"]
    assert hook.verify(headers, body) is None


@pytest.mark.parametrize("payload,expected", [
    ({"event": "infrahub.node.updated", "data": {"kind": "NetworkDevice"}}, True),
    ({"event": "infrahub.node.created", "data": {"kind": "NetworkService"}}, True),
    ({"event": "infrahub.node.deleted", "data": {"kind": "BuiltinTag"}}, True),
    ({"event": "infrahub.node.updated", "data": {"kind": "CoreAccount"}}, False),
    ({"event": "infrahub.branch.merged", "data": {}}, True),
    ({"event": "infrahub.validator.passed", "data": {}}, False),
])
def test_only_changes_that_affect_render_are_relevant(hook, payload, expected):
    assert hook.relevant(payload) is expected


def test_a_burst_of_events_renders_once(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, "DEBOUNCE", 0.2)
    monkeypatch.setattr(hook, "_render", lambda: calls.append(time.time()))
    for _ in range(25):
        hook.schedule()
    time.sleep(0.6)
    assert len(calls) == 1


def test_refuses_to_serve_without_a_secret(monkeypatch):
    monkeypatch.setenv("RENDER_HOOK_SECRET", "")
    spec = importlib.util.spec_from_file_location("rh", ROOT / "source-of-truth/scripts/render-hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 2


def test_hook_is_optional_internal_and_renders_like_make_render():
    services = yaml.safe_load((ROOT / "compose/source-of-truth.yaml").read_text())["services"]
    hook = services["render-hook"]
    assert hook["profiles"] == ["auto-render"], "auto-render must be off unless asked for"
    assert "ports" not in hook, "the hook rewrites collector config: never publish it"
    assert "../observability/telegraf/generated:/generated" in hook["volumes"]
    env = hook["environment"]
    for var in ("SNMP_SHARD_SIZE", "FLOW_DEDICATED_FIRST", "FLOW_DEDICATED_LAST"):
        assert var in env, f"render-hook must render with the same {var} as make render"
