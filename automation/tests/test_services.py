"""Layer 2 — does each service answer its own readiness endpoint?

A container can be running while the process inside is not ready, or is ready
but misconfigured. This asks each component directly.

Skips when the stack is not up.
"""
from __future__ import annotations

import pytest

from automation.tests.conftest import requires_profile


def test_prometheus_is_ready(env, http, compose_ps):
    assert "Prometheus" in str(http(f"http://localhost:{env['PROMETHEUS_PORT']}/-/ready", retries=3))


def test_loki_is_ready(env, http, compose_ps):
    http(f"http://localhost:{env['LOKI_PORT']}/ready", retries=3)


def test_alertmanager_is_ready(env, http, compose_ps):
    http(f"http://localhost:{env['ALERTMANAGER_PORT']}/-/ready", retries=3)


def test_grafana_database_is_ok(env, http, compose_ps):
    health = http(f"http://localhost:{env['GRAFANA_PORT']}/api/health", retries=3)
    assert health.get("database") == "ok", health


def test_infrahub_answers(env, http, compose_ps):
    http(f"http://localhost:{env['INFRAHUB_PORT']}/api/schema/summary", retries=3)


def test_automation_api_answers(env, http, compose_ps):
    requires_profile(env, "automation")
    assert http(f"http://localhost:{env['AUTOMATION_PORT']}/healthz", retries=3)["status"] == "ok"
