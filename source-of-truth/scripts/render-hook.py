#!/usr/bin/env python3
"""Auto-render: re-run render-inventory.py when Infrahub's main branch changes.

Off by default. Enabled with the `auto-render` compose profile (site.yml
`source_of_truth.auto_render: true`), then registered once with
`make auto-render`. Without it, `make render` after every change is still the
way collectors pick changes up — exactly as before.

    Infrahub (main) --webhook, HMAC-signed--> render-hook --> render-inventory.py
                                                              (debounced)

Why debounced: `make seed` of 40 devices is 40+ node events in a few seconds.
Rendering once after the burst settles gives the same result as rendering 40
times, without 40 Telegraf reloads.

Why signed: the hook rewrites collector configuration. Infrahub signs each
request (Standard Webhooks: HMAC-SHA256 over "<id>.<timestamp>.<body>" with
the shared key); anything unsigned, mis-signed or older than five minutes is
refused. The port is `expose:` only — reachable on the compose network, never
published.

Runs in the Infrahub image (it already has the SDK the renderer needs):
    python /scripts/render-hook.py               serve on :8099
    python /scripts/render-hook.py --register    create/update the Infrahub webhook
    python /scripts/render-hook.py --unregister  deactivate it
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("RENDER_HOOK_SECRET", "")
DEBOUNCE = float(os.environ.get("RENDER_HOOK_DEBOUNCE", "15"))
PORT = int(os.environ.get("RENDER_HOOK_PORT", "8099"))
HOOK_URL = os.environ.get("RENDER_HOOK_URL", f"http://render-hook:{PORT}/hook")
WEBHOOK_NAME = "darqcube-auto-render"
RENDERER = os.environ.get("RENDERER", "/scripts/render-inventory.py")
MAX_AGE = 300            # seconds a signed request stays valid
MAX_BODY = 1_048_576     # Infrahub events are small; refuse anything absurd

# Only changes that can alter what render writes. Everything else Infrahub
# emits (validators, artifacts, threads, ...) is acknowledged and ignored.
RELEVANT_EVENTS = ("infrahub.node.", "infrahub.branch.merged", "infrahub.proposed_change.merged",
                   "infrahub.schema.updated")
RELEVANT_KINDS = ("Network", "BuiltinTag")

state = {"last_render": None, "last_ok": None, "last_output": "", "pending": False, "renders": 0}
_lock = threading.Lock()
_timer: threading.Timer | None = None


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} render-hook: {msg}", flush=True)


# --- signature -------------------------------------------------------------

def verify(headers, body: bytes, secret: str = SECRET, now: float | None = None) -> str | None:
    """None when the request is authentic, else the reason it is not.

    Infrahub signs the payload serialised compactly (separators ",", ":"), but
    the HTTP body may be serialised differently — so the body is parsed and
    re-serialised the same way before checking, key order preserved.
    """
    msg_id = headers.get("webhook-id")
    stamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature", "")
    if not (msg_id and stamp and signatures):
        return "unsigned request"
    try:
        age = abs((now or time.time()) - int(stamp))
    except ValueError:
        return "bad timestamp"
    if age > MAX_AGE:
        return f"stale request ({int(age)} s old)"
    try:
        compact = json.dumps(json.loads(body or b"{}"), separators=(",", ":"))
    except ValueError:
        return "body is not JSON"
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{msg_id}.{stamp}.{compact}".encode(), hashlib.sha256).digest()
    ).decode()
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return None
    return "bad signature"


def relevant(payload: dict) -> bool:
    """Does this event change anything render writes?"""
    event = str(payload.get("event") or payload.get("event_type") or "")
    data = payload.get("data") or {}
    kind = str(data.get("kind") or data.get("node_kind") or "")
    if event.startswith("infrahub.node."):
        return kind.startswith(RELEVANT_KINDS) if kind else True
    return event.startswith(RELEVANT_EVENTS) if event else True


# --- debounced render ------------------------------------------------------

def _render() -> None:
    global _timer
    with _lock:
        _timer = None
        state["pending"] = False
    started = time.time()
    proc = subprocess.run([sys.executable, RENDERER], capture_output=True, text=True, timeout=300)
    out = (proc.stdout + proc.stderr).strip()
    with _lock:
        state.update(last_render=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     last_ok=proc.returncode == 0, last_output=out[-2000:], renders=state["renders"] + 1)
    log(f"render {'ok' if proc.returncode == 0 else f'FAILED (exit {proc.returncode})'} "
        f"in {time.time() - started:.1f}s")
    if proc.returncode != 0:
        log(out[-800:])


def schedule() -> None:
    """(Re)start the debounce window: the render runs DEBOUNCE s after the last event."""
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE, _render)
        _timer.daemon = True
        _timer.start()
        state["pending"] = True


class Handler(BaseHTTPRequestHandler):
    server_version = "render-hook"

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            with _lock:
                return self._reply(200, {k: v for k, v in state.items() if k != "last_output"})
        self._reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/hook":
            return self._reply(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._reply(413, {"error": "too large"})
        body = self.rfile.read(length)
        problem = verify(self.headers, body)
        if problem:
            log(f"refused: {problem}")
            return self._reply(401, {"error": problem})
        payload = json.loads(body or b"{}")
        data = payload.get("data") or {}
        what = f"{payload.get('event') or payload.get('event_type') or '?'} {data.get('kind') or ''}".strip()
        if not relevant(payload):
            log(f"ignored {what}")
            return self._reply(202, {"ignored": True})
        log(f"accepted {what} — render in {DEBOUNCE:g}s")
        schedule()
        self._reply(202, {"scheduled_in_s": DEBOUNCE})

    def log_message(self, fmt, *args):  # keep the log to what matters
        pass


# --- registration ----------------------------------------------------------

def _client():
    from infrahub_sdk import Config, InfrahubClientSync
    token = os.environ.get("INFRAHUB_API_TOKEN") or os.environ.get("INFRAHUB_INITIAL_ADMIN_TOKEN")
    if not token:
        raise SystemExit("!! INFRAHUB_API_TOKEN not set")
    address = os.environ.get("INFRAHUB_ADDRESS", "http://infrahub-server:8000")
    return InfrahubClientSync(address=address, config=Config(api_token=token))


def register(active: bool = True) -> int:
    if active and not SECRET:
        print("!! RENDER_HOOK_SECRET is empty — set it in .env first", file=sys.stderr)
        return 2
    client = _client()
    existing = client.filters(kind="CoreStandardWebhook", name__value=WEBHOOK_NAME)
    if not active:
        for hook in existing:
            hook.active.value = False
            hook.save()
        print(f"{WEBHOOK_NAME}: deactivated" if existing else f"{WEBHOOK_NAME}: not registered")
        return 0
    fields = dict(url=HOOK_URL, event_type="all", branch_scope="default_branch",
                  shared_key=SECRET, active=True, validate_certificates=False,
                  description="Re-render collector config on main-branch changes (render-hook)")
    if existing:
        hook = existing[0]
        for key, value in fields.items():
            getattr(hook, key).value = value
        hook.save()
        print(f"{WEBHOOK_NAME}: updated -> {HOOK_URL}")
    else:
        client.create(kind="CoreStandardWebhook", name=WEBHOOK_NAME, **fields).save()
        print(f"{WEBHOOK_NAME}: registered -> {HOOK_URL}")
    return 0


def main(argv: list[str]) -> int:
    if "--register" in argv:
        return register(True)
    if "--unregister" in argv:
        return register(False)
    if not SECRET:
        print("!! RENDER_HOOK_SECRET is empty — refusing to accept unsigned webhooks", file=sys.stderr)
        return 2
    log(f"listening on :{PORT}, debounce {DEBOUNCE:g}s")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
