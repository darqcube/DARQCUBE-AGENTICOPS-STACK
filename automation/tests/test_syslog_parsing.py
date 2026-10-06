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
import re
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


LATE_MIN, SKEW_MIN = 10, 120


def cisco_at(host: str, minutes_ago: int) -> str:
    """A Cisco line stamped `minutes_ago` before now, UTC — generated at run
    time, because event-time handling is relative to arrival."""
    import datetime as dt
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    stamp = t.strftime("%b %d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"
    return f"<189>7: {host}: {stamp} UTC: %SYS-5-CONFIG_I: Configured from console by admin on vty0\n"


def generated_lines() -> str:
    return cisco_at("late", LATE_MIN) + cisco_at("skewed", SKEW_MIN)


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    """Run the real filter block over the samples, return events by device."""
    if not _image_exists():
        pytest.skip(f"{IMAGE} not built — run: docker compose build logstash")

    work = tmp_path_factory.mktemp("ls")
    (work / "pipeline").mkdir()
    # pytest makes its temp dirs 0700. Logstash runs as uid 1000, so on any
    # host whose user is not uid 1000 it cannot open devices.yml and the
    # pipeline refuses to start. Open the dirs up; the files default to 0644.
    for d in (work, work / "pipeline"):
        d.chmod(0o755)

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
        "router1: router1|hq|core|ios_xe\n"
        "sw-hw-01: sw-hw-01|hq|access|vrp\n"
        "mt-01: mt-01|branch-01|wan|routeros\n"
        "late: late|hq|core|ios_xe\n"
        "skewed: skewed|hq|core|ios_xe\n"
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
        input=SAMPLES.read_text() + generated_lines(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    events = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.startswith("{")
    ]
    # Logstash logs to stdout, so its errors are there, not in stderr.
    assert events, (
        f"logstash produced no events:\n{(result.stdout + result.stderr)[-2000:]}"
    )
    return {e.get("device"): e for e in events}


def test_every_sample_produces_an_event(parsed):
    """Nothing is dropped — including the line nothing matches."""
    assert len(parsed) == len(
        [l for l in (SAMPLES.read_text() + generated_lines()).splitlines() if l.strip()]
    )


def test_cisco_is_parsed(parsed):
    """IOS is not RFC3164: counter and hostname precede the timestamp."""
    e = parsed["router1"]
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
    assert parsed["router1"]["site"] == "hq"
    assert parsed["router1"]["role"] == "core"
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


# --- the line Loki stores ----------------------------------------------------
# Loki keeps one field as the log line. It used to be `msg` alone, so the
# mnemonic was neither a label nor in the line: `|= "CONFIG_I"` matched nothing
# although the event had arrived. The line now carries it, in vendor notation.

def test_cisco_line_keeps_the_mnemonic(parsed):
    assert parsed["router1"]["line"] == "%SSH-5-SSH2_SESSION: SSH2 Session request from 10.0.0.5"


def test_huawei_line_keeps_the_mnemonic(parsed):
    assert parsed["sw-hw-01"]["line"].startswith("IFNET/4/LINK_STATE: ")
    assert "GE0/0/1" in parsed["sw-hw-01"]["line"]


def test_mikrotik_line_keeps_the_topics(parsed):
    assert parsed["mt-01"]["line"].startswith("system,info: user admin logged in")


def test_unparsed_line_is_stored_as_is(parsed):
    assert parsed["unparsed"]["line"] == "this line matches nothing in particular"


def test_loki_stores_the_rebuilt_line():
    """Pointing message_field back at `msg` silently drops every mnemonic."""
    text = PIPELINE.read_text()
    assert 'message_field => "line"' in text


# --- transport ----------------------------------------------------------------
# UDP loses a datagram silently anywhere on the path; bursts go first. TCP is
# offered on the same port so a device can switch without any stack change.

def test_syslog_is_received_on_udp_and_tcp_on_the_same_port():
    text = PIPELINE.read_text()
    inputs = text[text.index("input {"):text.index("filter {")]
    assert re.search(r"udp\s*\{[^}]*port => 514", inputs, re.S)
    assert re.search(r"tcp\s*\{[^}]*port => 514", inputs, re.S)
    # IOS frames syslog over TCP as one message per line (captured from a real
    # device); octet-counted framing would need a different codec.
    assert re.search(r"tcp\s*\{[^}]*codec => line", inputs, re.S)


def test_compose_publishes_syslog_on_both_transports():
    import yaml

    svc = yaml.safe_load((ROOT / "compose/observability.yaml").read_text())["services"]["logstash"]
    assert "${SYSLOG_PORT:-514}:514/udp" in svc["ports"]
    assert "${SYSLOG_PORT:-514}:514/tcp" in svc["ports"]


def test_source_ip_fallback_covers_tcp():
    """The TCP input keeps the sender only in @metadata; without the copy the
    by-IP fallback silently never matches for TCP senders."""
    text = PIPELINE.read_text()
    assert '"[@metadata][input][tcp][source][ip]" => "[host][ip]"' in text


def test_ip_fallback_can_overwrite_the_failed_name_lookup():
    """The name lookup writes its fallback "" to [sot] first; translate never
    replaces an existing target unless told to. Without override the by-IP
    match was discarded and the fallback never worked, on either transport —
    verified against the real image with a hostname absent from the table."""
    text = PIPELINE.read_text()
    by_ip = text[text.index('source => "[host][ip]"'):]
    by_ip = by_ip[:by_ip.index("}")]
    assert "override => true" in by_ip


# --- event time ----------------------------------------------------------------
# Loki files a line under @timestamp. Arrival time misplaces a line that came in
# late (TCP retransmits, device buffering); the device's own time is used when it
# names its zone and is within 30 min before / 5 min after arrival.

def _age_minutes(event) -> float:
    import datetime as dt
    ts = dt.datetime.fromisoformat(event["@timestamp"].replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60


def test_late_line_is_filed_at_the_device_time(parsed):
    e = parsed["late"]
    assert "clock_skew" not in e.get("tags", [])
    assert abs(_age_minutes(e) - LATE_MIN) < 2, e["@timestamp"]


def test_implausible_device_time_keeps_arrival_and_is_tagged(parsed):
    """Two hours off is a wrong clock, not a late line — and Loki would reject
    a line that old against a live stream."""
    e = parsed["skewed"]
    assert "clock_skew" in e["tags"]
    assert _age_minutes(e) < 5


def test_sample_from_months_ago_is_tagged_not_backdated(parsed):
    assert "clock_skew" in parsed["router1"]["tags"]
    assert _age_minutes(parsed["router1"]) < 5


def test_zoneless_timestamp_keeps_arrival_time(parsed):
    """RouterOS sends local time with no zone — never guessed at."""
    e = parsed["mt-01"]
    assert "clock_skew" not in e.get("tags", [])
    assert _age_minutes(e) < 5
