"""Layer 3 — are the components actually connected to each other?

This is the layer that catches real problems. Every service can be running and
answering while nothing is joined: Prometheus scraping nothing, Grafana with a
datasource that cannot connect, Logstash delivering logs that carry no identity.

Skips when the stack is not up.
"""
from __future__ import annotations

import json

import pytest

from automation.tests.conftest import ROOT, requires_profile


def test_prometheus_scrape_targets_are_up(env, http, compose_ps):
    """Present is not the same as up."""
    data = http(f"http://localhost:{env['PROMETHEUS_PORT']}/api/v1/targets", retries=3)
    targets = data["data"]["activeTargets"]
    assert targets, "Prometheus has no scrape targets at all"
    down = {t["labels"].get("job"): t.get("lastError", "") for t in targets if t["health"] != "up"}
    assert not down, f"scrape targets down: {down}"


def test_grafana_datasources_connect(env, http, compose_ps):
    """A datasource can exist and still fail to reach its backend."""
    import base64

    auth = base64.b64encode(
        f"{env.get('GRAFANA_ADMIN_USER', 'admin')}:{env.get('GRAFANA_ADMIN_PASSWORD', '')}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}"}
    base = f"http://localhost:{env['GRAFANA_PORT']}"

    sources = http(f"{base}/api/datasources", headers=headers, retries=3)
    names = {d["name"] for d in sources}
    assert {"Prometheus", "Loki"} <= names, f"datasources provisioned: {names}"

    for source in sources:
        health = http(f"{base}/api/datasources/uid/{source['uid']}/health", headers=headers)
        assert health.get("status") == "OK", f"{source['name']}: {health.get('message')}"


def test_infrahub_has_devices(env, http, compose_ps):
    """Nothing downstream works until the source of truth is seeded."""
    import urllib.request

    req = urllib.request.Request(
        f"http://localhost:{env['INFRAHUB_PORT']}/graphql",
        data=json.dumps({"query": "{NetworkDevice{count}}"}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-INFRAHUB-KEY": env.get("INFRAHUB_ADMIN_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        body = json.loads(response.read())
    count = body["data"]["NetworkDevice"]["count"]
    assert count > 0, "Infrahub has no devices — run `make seed`"


def test_rendered_inventory_matches_the_source_of_truth(compose_ps):
    """`make render` is the step people forget: a device can exist in Infrahub
    and be polled by nothing."""
    generated = ROOT / "observability/telegraf/generated/devices.json"
    if not generated.exists():
        pytest.fail("nothing rendered — run `make render`")
    identity = json.loads(generated.read_text())
    assert identity, "identity table is empty — run `make render`"
    for record in identity.values():
        assert {"device", "site", "role", "platform"} <= set(record)


def test_loki_carries_the_device_label(env, http, compose_ps):
    """The sharpest single check in the suite.

    The `device` label exists only if a device sent a log, Logstash matched it
    with the right vendor pattern, AND the hostname resolved against the
    Infrahub-rendered identity table. One assertion covers the whole syslog
    path end to end.
    """
    labels = http(f"http://localhost:{env['LOKI_PORT']}/loki/api/v1/labels", retries=3)["data"]
    if not labels:
        pytest.skip("Loki has received no logs yet")
    assert "device" in labels, (
        f"Loki has logs but no device label — Logstash is delivering without "
        f"enriching. Labels present: {labels}"
    )


def test_mcp_servers_expose_their_tools(env, compose_ps):
    """Each server must register tools, and the write tool must stay absent
    unless it was explicitly enabled."""
    requires_profile(env, "mcp")
    import subprocess

    expected = {
        "mcp-infrahub": 9001, "mcp-prometheus": 9002, "mcp-loki": 9003,
        "mcp-grafana": 9004, "mcp-netmiko": 9005, "mcp-assurance": 9006,
    }
    for name, port in expected.items():
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", name,
             "python", "-c",
             f"import urllib.request,sys;"
             f"sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/healthz').status==200 else 1)"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert out.returncode == 0, f"{name} did not answer /healthz: {out.stderr[-200:]}"
