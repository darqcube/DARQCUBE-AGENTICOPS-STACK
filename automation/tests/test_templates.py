"""TextFSM template tests — offline, no stack, no devices, ~1 second.

Run these first after touching anything parsing-related: `make test-templates`.

Each sample file is real CLI output captured from a device. The filename
encodes the platform and command, so adding a template means adding a sample
and the test picks it up automatically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "automation/textfsm/samples"
sys.path.insert(0, str(ROOT))

# Absolute import: `import parse` would work by accident here but breaks the
# real textfsm library elsewhere. See automation/__init__.py.
from automation.textfsm import parse  # noqa: E402


MANIFEST = yaml.safe_load((SAMPLES / "manifest.yml").read_text())["samples"]


def sample_ids():
    return [s["file"] for s in MANIFEST]


@pytest.mark.parametrize("entry", MANIFEST, ids=sample_ids())
def test_sample_parses_into_rows(entry):
    """The core guarantee: real output becomes structured rows."""
    raw = (SAMPLES / entry["file"]).read_text()
    rows = parse.parse_output(entry["platform"], entry["command"], raw)
    assert rows, f"{entry['file']} parsed to nothing"
    assert isinstance(rows[0], dict)


@pytest.mark.parametrize("entry", MANIFEST, ids=sample_ids())
def test_sample_yields_the_expected_fields(entry):
    """A template that parses but drops a column is worse than one that fails,
    because the result still looks like data."""
    raw = (SAMPLES / entry["file"]).read_text()
    rows = parse.parse_output(entry["platform"], entry["command"], raw)
    missing = set(entry["expect_keys"]) - set(rows[0])
    assert not missing, f"{entry['file']}: missing {missing}, got {sorted(rows[0])}"


@pytest.mark.parametrize("entry", MANIFEST, ids=sample_ids())
def test_sample_file_exists_and_platform_is_known(entry):
    assert (SAMPLES / entry["file"]).exists()
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    assert entry["platform"] in platforms


def test_every_platform_state_cmd_has_a_verified_sample():
    """platforms.yml `state_cmd` must be a command a template exists for.

    This catches the two traps found while building this stack, both of which
    fail silently: ntc-templates has NO cisco_xe templates (IOS-XE is filed
    under cisco_ios), and it has no plain "/interface print" for RouterOS —
    only the brief, detail and terse variants.
    """
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    by_pair = {(s["platform"], s["command"]): s for s in MANIFEST}
    for name, spec in platforms.items():
        entry = by_pair.get((name, spec["state_cmd"]))
        assert entry, (
            f"{name}: state_cmd {spec['state_cmd']!r} has no sample in "
            f"manifest.yml — it is unverified and may fail on a real device"
        )
        raw = (SAMPLES / entry["file"]).read_text()
        assert parse.parse_output(name, spec["state_cmd"], raw)


def test_empty_output_raises_rather_than_returning_nothing():
    """[] and 'the device has nothing to report' are indistinguishable, so an
    unparseable response must fail loudly instead of looking healthy."""
    with pytest.raises(parse.ParseError):
        parse.parse_output("ios_xe", "show ip interface brief", "")


def test_unmatched_output_raises():
    with pytest.raises(parse.ParseError, match="matched no lines|no template"):
        parse.parse_output("ios_xe", "show ip interface brief", "not interface output at all\n")


def test_unknown_platform_raises_with_a_useful_message():
    with pytest.raises(parse.ParseError, match="not in platforms.yml"):
        parse.parse_output("nx_os", "show version", "anything")
