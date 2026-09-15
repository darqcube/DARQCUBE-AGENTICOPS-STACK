"""Syslog parsing tests — runs Logstash's real filter block against captured
wire-format samples from each vendor.

Why this matters more than it looks: a grok pattern that stops matching does
NOT raise. The line falls through to the catch-all, loses its device/site/role
labels, and lands in Loki looking almost right. Nothing reports it. These tests
are the only thing that turns that into a visible failure.

Needs the darqcube/logstash:local image (docker compose build logstash) but no
running stack and no devices.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "observability/logstash/pipeline/syslog.conf"
PATTERNS = ROOT / "observability/logstash/patterns"
SAMPLES = ROOT / "observability/logstash/samples/syslog-samples.txt"
IMAGE = "darqcube/logstash:local"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker not available"
)


def _image_exists() -> bool:
    out = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True
    )
    return out.returncode == 0


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    """Run the real filter block over the samples, return events by device."""
    if not _image_exists():
        pytest.skip(f"{IMAGE} not built — run: docker compose build logstash")

    work = tmp_path_factory.mktemp("ls")
    (work / "pipeline").mkdir()

    # Reuse the production filter block verbatim; only input and output are
    # swapped, so a test can never pass against a pipeline the stack doesn't use.
    src = PIPELINE.read_text()
    body = src[src.index("filter {") : src.index("output {")]
    (work / "pipeline/test.conf").write_text(
        "input { stdin { codec => line } }\n"
        + body
        + "output { stdout { codec => json_lines } }\n"
    )

    # The identity table render-inventory.py would produce for these devices.
    (work / "devices.yml").write_text(
        "cr1: cr1|hq|core|ios_xe\n"
        "sw-hw-01: sw-hw-01|hq|access|vrp\n"
        "mt-01: mt-01|branch-01|wan|routeros\n"
    )

    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "-v", f"{work / 'pipeline'}:/usr/share/logstash/pipeline:ro",
            "-v", f"{PATTERNS}:/usr/share/logstash/patterns:ro",
            "-v", f"{work}:/usr/share/logstash/lookup:ro",
            "-e", "LS_JAVA_OPTS=-Xms512m -Xmx512m",
            IMAGE, "logstash",
        ],
        input=SAMPLES.read_text(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    events = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.startswith("{")
    ]
    assert events, f"logstash produced no events:\n{result.stderr[-2000:]}"
    return {e.get("device"): e for e in events}


def test_every_sample_produces_an_event(parsed):
    """Nothing is dropped — including the line nothing matches."""
    assert len(parsed) == len(
        [l for l in SAMPLES.read_text().splitlines() if l.strip()]
    )


def test_cisco_is_parsed(parsed):
    """IOS is not RFC3164: counter and hostname precede the timestamp."""
    e = parsed["cr1"]
    assert e["platform"] == "ios_xe"
    assert e["facility"] == "SSH"
    assert e["mnemonic"] == "SSH2_SESSION"
    assert e["msg"] == "SSH2 Session request from 10.0.0.5"


def test_huawei_is_parsed(parsed):
    """VRP packs module/severity/mnemonic into a %%NN prefix."""
    e = parsed["sw-hw-01"]
    assert e["platform"] == "vrp"
    assert e["facility"] == "IFNET"
    assert e["mnemonic"] == "LINK_STATE"
    assert e["severity"] == "warning"          # from the -4- in the prefix
    assert "GE0/0/1" in e["msg"]


def test_mikrotik_is_parsed(parsed):
    """RouterOS has no severity in the body — it comes from the PRI."""
    e = parsed["mt-01"]
    assert e["platform"] == "routeros"
    assert e["severity"] == "info"             # PRI 134 % 8 == 6
    assert e["msg"].startswith("user admin logged in")


def test_source_of_truth_labels_are_attached(parsed):
    """The interconnect: labels come from Infrahub, not from the log line."""
    assert parsed["cr1"]["site"] == "hq"
    assert parsed["cr1"]["role"] == "core"
    assert parsed["mt-01"]["site"] == "branch-01"
    assert parsed["mt-01"]["role"] == "wan"


def test_unparseable_line_is_kept_and_tagged(parsed):
    """A log you cannot parse is worth more than one you threw away."""
    e = parsed["unparsed"]
    assert e["site"] == "unknown"
    assert "not_in_source_of_truth" in e["tags"]
    assert e["msg"] == "this line matches nothing in particular"


def test_high_cardinality_fields_never_become_labels():
    """Loki labels must stay bounded. mnemonic/topics/msg are per-message, so
    promoting one multiplies stream count by the number of message types and
    makes Loki unusable at a point where it is expensive to undo."""
    text = PIPELINE.read_text()
    line = next(l for l in text.splitlines() if "include_fields" in l)
    allowed = {"device", "site", "role", "severity", "platform"}
    declared = set(line.split("[")[1].split("]")[0].replace('"', "").replace(" ", "").split(","))
    assert declared == allowed, f"Loki label set changed: {declared}"
