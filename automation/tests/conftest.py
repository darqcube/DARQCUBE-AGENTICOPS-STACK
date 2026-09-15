"""Shared fixtures for the stack tests.

The offline suites (render, templates, syslog parsing, assurance, docs) need
none of this. The three stack layers below do, and they SKIP rather than fail
when the stack is not running — so `make test-templates` and the rest stay
useful on a laptop with nothing up.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict[str, str]:
    """Values from .env, with the same defaults the compose files use."""
    values = {
        "GRAFANA_PORT": "3000", "INFRAHUB_PORT": "8000",
        "PROMETHEUS_PORT": "9090", "ALERTMANAGER_PORT": "9093",
        "LOKI_PORT": "3100", "AUTOMATION_PORT": "8100",
        "INFRAHUB_ADMIN_TOKEN": "", "MCP_AUTH_TOKEN": "",
        "COMPOSE_PROFILES": "",
    }
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                values[key.strip()] = val.split("#")[0].strip()
    return values


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return _env()


@pytest.fixture(scope="session")
def compose_ps():
    """`docker compose ps` as a list of dicts, or skip if nothing is running."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    if not (ROOT / ".env").exists():
        pytest.skip("no .env — copy .env.example and run `make up` to run the stack tests")

    out = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        # Usually a missing or incomplete .env: compose refuses to resolve the
        # config before it ever looks at what is running.
        detail = (out.stderr or "").strip().splitlines()
        pytest.skip(f"docker compose could not read the project: {detail[-1] if detail else '?'}")

    # Compose emits either a JSON array or one object per line, by version.
    text = out.stdout.strip()
    if not text:
        pytest.skip("stack is not running — start it with `make up`")
    try:
        data = json.loads(text)
        services = data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        services = [json.loads(l) for l in text.splitlines() if l.strip()]
    if not services:
        pytest.skip("stack is not running — start it with `make up`")
    return services


@pytest.fixture(scope="session")
def http():
    """A GET helper that polls, because a service can be healthy-but-warming."""
    import urllib.error
    import urllib.request

    def get(url: str, timeout: float = 5.0, retries: int = 1, headers=None):
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    body = response.read().decode()
                    try:
                        return json.loads(body)
                    except json.JSONDecodeError:
                        return body
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(2)
        raise AssertionError(f"{url} did not answer: {last}")

    return get


def requires_profile(env: dict[str, str], profile: str) -> None:
    if profile not in env.get("COMPOSE_PROFILES", ""):
        pytest.skip(f"profile '{profile}' is not enabled in COMPOSE_PROFILES")
