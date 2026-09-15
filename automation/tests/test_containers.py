"""Layer 1 — are the containers actually running?

The cheapest check, and the one that catches a crash-loop, a container that
exited 0 and was forgotten, or an OOM kill.

Skips when the stack is not up.
"""
from __future__ import annotations

EXPECTED_GROUPS = {"source-of-truth", "observability", "automation", "mcp"}


def test_no_service_has_exited(compose_ps):
    bad = [s for s in compose_ps if s.get("State") != "running"]
    assert not bad, "not running: " + ", ".join(
        f"{s.get('Service')}={s.get('State')}" for s in bad
    )


def test_every_service_with_a_healthcheck_is_healthy(compose_ps):
    """`make up` uses --wait, so an unhealthy container here means it degraded
    after startup rather than never having come up."""
    unhealthy = [
        s for s in compose_ps
        if s.get("Health") not in (None, "", "healthy")
    ]
    assert not unhealthy, "unhealthy: " + ", ".join(
        f"{s.get('Service')}={s.get('Health')}" for s in unhealthy
    )


def test_core_services_are_present(compose_ps):
    running = {s.get("Service") for s in compose_ps}
    core = {"infrahub-server", "prometheus", "loki", "grafana", "alertmanager", "logstash"}
    missing = core - running
    assert not missing, f"core services not started: {missing}"
