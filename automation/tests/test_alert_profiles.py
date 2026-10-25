"""Alert profiles (observability/prometheus/profiles/): one is loaded, both define the same alerts, standard keeps the
long-standing timing, demo clears within minutes. promtool proves the difference on the same fault and fix."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "observability/prometheus/profiles"


def alerts(profile: str) -> dict[str, dict]:
    doc = yaml.safe_load((PROFILES / f"{profile}.yml").read_text())
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"]}


def test_both_profiles_define_the_same_alerts():
    assert set(alerts("standard")) == set(alerts("demo")) == {"AssuranceCheckFailing", "IPSLAProbeFailing"}


def test_no_alert_is_defined_twice():
    base = yaml.safe_load((ROOT / "observability/prometheus/rules/alerts.yml").read_text())
    names = [r["alert"] for g in base["groups"] for r in g["rules"] if "alert" in r]
    assert not set(names) & set(alerts("standard")), "a profile alert is still in rules/alerts.yml"


def test_standard_keeps_the_long_standing_timing():
    a = alerts("standard")
    assert a["AssuranceCheckFailing"]["for"] == "20m"
    assert a["IPSLAProbeFailing"]["expr"] == "ipsla:success:ratio15m < 0.8" and a["IPSLAProbeFailing"]["for"] == "5m"


def test_demo_is_minutes():
    a = alerts("demo")
    assert a["AssuranceCheckFailing"]["for"] == "2m"
    assert a["IPSLAProbeFailing"]["expr"] == "ipsla:success:ratio5m < 0.8" and a["IPSLAProbeFailing"]["for"] == "1m"


def test_compose_mounts_one_profile_and_prometheus_loads_it():
    compose = (ROOT / "compose/observability.yaml").read_text()
    assert "../observability/prometheus/profiles/${ALERT_PROFILE:-standard}.yml:/etc/prometheus-profile/alerts.yml:ro" in compose
    prom = yaml.safe_load((ROOT / "observability/prometheus/prometheus.yml").read_text())
    assert "/etc/prometheus-profile/*.yml" in prom["rule_files"]
    assert "ALERT_PROFILE=standard" in (ROOT / ".env.example").read_text()


@pytest.mark.parametrize("profile", ["standard", "demo"])
def test_promtool_fault_and_fix(profile):
    """A probe fails for 30 min, then recovers. 6 min after the fix the demo alert has cleared; standard still fires."""
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    version = re.search(r"^PROMETHEUS_VERSION=(\S+)", (ROOT / ".env.example").read_text(), re.M)
    image = f"prom/prometheus:{version.group(1)}" if version else "prom/prometheus"
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT / 'observability/prometheus'}:/p:ro", "--entrypoint",
                        "promtool", image, "test", "rules", f"/p/tests/profile-{profile}.test.yml"],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
