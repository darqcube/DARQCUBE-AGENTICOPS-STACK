"""Site file tests — the per-deployment input that generates .env.

Offline: no Docker, no stack. The site file is how every customer deployment is
configured, so its reader, validation and derivation all get pinned here.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "site.example.yml"


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def site(inst):
    """The example, with the placeholders filled in."""
    text = EXAMPLE.read_text()
    text = (text.replace("ssh_password: CHANGEME", "ssh_password: pw")
                .replace("auth: CHANGEME", "auth: authpw")
                .replace("priv: CHANGEME", "priv: privpw"))
    tmp = ROOT / ".site-test.yml"
    tmp.write_text(text)
    try:
        yield inst.read_yaml(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# --- the reader ------------------------------------------------------------

def test_reader_agrees_with_pyyaml(inst):
    """install.py parses the site file with a small reader of its own, because
    it must run before pip has been used and PyYAML is not stdlib. This proves
    the subset it supports is genuinely YAML rather than something that merely
    looks like it."""
    assert inst.read_yaml(EXAMPLE) == yaml.safe_load(EXAMPLE.read_text())


def test_reader_handles_the_types_the_file_uses(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(
        "# a comment\n"
        "top:\n"
        "  text: hello\n"
        '  quoted: "with spaces"\n'
        "  number: 42\n"
        "  yes_flag: true\n"
        "  no_flag: false\n"
        "  empty: \"\"\n"
        "  items:\n"
        "    - one\n"
        "    - two\n"
    )
    got = inst.read_yaml(f)
    assert got == yaml.safe_load(f.read_text())
    assert got["top"]["number"] == 42 and got["top"]["yes_flag"] is True


def test_reader_rejects_a_malformed_line(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text("site:\n  this line has no colon\n")
    with pytest.raises(inst.SiteError, match="expected 'key: value'"):
        inst.read_yaml(f)


# --- validation ------------------------------------------------------------

def test_the_shipped_example_is_only_missing_its_secrets(inst):
    """Everything except the three CHANGEME credentials must already be valid,
    so the first thing an engineer sees is a short, specific list."""
    problems = inst.validate_site(inst.read_yaml(EXAMPLE))
    creds = [p for p in problems if "CHANGEME" in p or "is required" in p]
    others = [p for p in problems if p not in creds and "collector_ip" not in p]
    assert not others, f"the example has problems beyond its placeholders: {others}"


def test_loopback_collector_ip_is_refused(inst, site):
    site["site"]["collector_ip"] = "127.0.0.1"
    assert any("loopback" in p for p in inst.validate_site(site))


def test_collector_ip_not_on_this_host_is_refused(inst, site):
    """The most common deployment mistake: devices send into a black hole."""
    site["site"]["collector_ip"] = "10.255.255.254"
    problems = inst.validate_site(site)
    if inst.local_addresses():        # skip where interfaces cannot be read
        assert any("not an address on this machine" in p for p in problems)


def test_unknown_top_level_key_is_refused(inst, site):
    """A typo'd key would otherwise be silently ignored and the value never
    applied — the worst kind of configuration bug."""
    site["ai-platform"] = {"enabled": True}
    assert any("unknown top-level key" in p for p in inst.validate_site(site))


def test_missing_credentials_are_named_individually(inst, site):
    site["devices"]["ssh_password"] = "CHANGEME"
    site["devices"]["snmpv3"]["auth"] = ""
    problems = inst.validate_site(site)
    assert any("ssh_password" in p for p in problems)
    assert any("snmpv3.auth" in p for p in problems)


def test_bad_cidr_is_refused(inst, site):
    site["site"]["device_subnets"] = ["10.20.10.0/99"]
    assert any("not a valid CIDR" in p for p in inst.validate_site(site))


def test_beyond_the_tested_ceiling_warns(inst, site):
    site["scale"]["expected_devices"] = 5000
    assert any("tested ceiling" in p for p in inst.validate_site(site))


# --- derivation ------------------------------------------------------------

@pytest.mark.parametrize("count,interval,shard,concurrency", [
    (20, "30s", "150", "8"),
    (120, "60s", "150", "16"),
    (400, "60s", "150", "16"),
    (900, "120s", "200", "24"),
])
def test_scale_band_drives_the_collector_settings(inst, site, count, interval, shard, concurrency):
    """expected_devices exists so nobody has to read docs/scale.md and do
    arithmetic at a customer site."""
    site["scale"]["expected_devices"] = count
    env = inst.site_to_env(site)
    assert env["SNMP_INTERVAL"] == interval
    assert env["SNMP_SHARD_SIZE"] == shard
    assert env["AUTOMATION_CONCURRENCY"] == concurrency


def test_standard_ports_toggle(inst, site):
    site["ports"]["standard"] = True
    assert inst.site_to_env(site)["GRAFANA_PORT"] == "3000"
    site["ports"]["standard"] = False
    assert inst.site_to_env(site)["GRAFANA_PORT"] == "13000"


def test_port_overrides_win(inst, site):
    site["ports"]["overrides"] = {"syslog": 514}
    env = inst.site_to_env(site)
    assert env["SYSLOG_PORT"] == "514"
    assert env["GRAFANA_PORT"] == "13000"      # others still follow `standard`


def test_ai_platform_toggles_the_mcp_profile(inst, site):
    site["ai_platform"]["enabled"] = False
    assert "mcp" not in inst.site_to_env(site)["COMPOSE_PROFILES"]
    site["ai_platform"]["enabled"] = True
    assert "mcp" in inst.site_to_env(site)["COMPOSE_PROFILES"]


def test_write_access_is_off_unless_asked_for(inst, site):
    assert inst.site_to_env(site)["MCP_ALLOW_WRITE"] == "false"
    site["ai_platform"]["allow_write"] = True
    assert inst.site_to_env(site)["MCP_ALLOW_WRITE"] == "true"


# --- the contract between the two files ------------------------------------

def test_every_derived_key_exists_in_env_example(inst, site):
    """A derived setting with no matching variable would be silently dropped."""
    example = (ROOT / ".env.example").read_text()
    for key in inst.site_to_env(site):
        assert re.search(rf"^{key}=", example, re.M), (
            f"site_to_env produces {key}, which .env.example does not define"
        )


def test_the_site_file_covers_every_value_a_human_must_supply(inst, site):
    """If a REQUIRED value is not derivable from the site file, an engineer
    using one would still get stopped by a prompt."""
    derived = inst.site_to_env(site)
    for var in inst.REQUIRED:
        assert derived.get(var), f"{var} is required but the site file cannot supply it"


def test_no_secret_is_derived_from_the_site_file(inst, site):
    """The seven generated secrets must stay generated — a site file that could
    set them would invite reusing one across customers."""
    assert not (set(inst.site_to_env(site)) & set(inst.GENERATED))


def test_site_yml_is_gitignored():
    """It holds credentials."""
    ignored = (ROOT / ".gitignore").read_text()
    assert re.search(r"^site\.yml$", ignored, re.M), "site.yml is not gitignored"


# --- coercion -------------------------------------------------------------
# Quoting a boolean in YAML makes it a STRING, and a non-empty string is
# truthy. `allow_write: "false"` therefore ENABLED write access to devices —
# the exact opposite of what was written, with nothing to notice it by.

@pytest.mark.parametrize("literal", ['false', '"false"', "'false'", "no", '"no"', "off", "0"])
def test_falsey_spellings_are_all_false(inst, literal, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(f"ports:\n  standard: {literal}\nai_platform:\n  allow_write: {literal}\n")
    env = inst.site_to_env(inst.read_yaml(f))
    assert env["MCP_ALLOW_WRITE"] == "false", f"{literal} enabled AI write access"
    assert env["GRAFANA_PORT"] == "13000", f"{literal} selected standard ports"


@pytest.mark.parametrize("literal", ["true", '"true"', "yes", "on", "1"])
def test_truthy_spellings_are_all_true(inst, literal, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text(f"ports:\n  standard: {literal}\nai_platform:\n  allow_write: {literal}\n")
    env = inst.site_to_env(inst.read_yaml(f))
    assert env["MCP_ALLOW_WRITE"] == "true"
    assert env["GRAFANA_PORT"] == "3000"


def test_an_ambiguous_boolean_is_refused_not_guessed(inst, tmp_path):
    f = tmp_path / "s.yml"
    f.write_text("ai_platform:\n  allow_write: maybe\n")
    with pytest.raises(inst.SiteError, match="expected true or false"):
        inst.site_to_env(inst.read_yaml(f))


def test_a_bad_device_count_is_reported_not_thrown(inst, site):
    """validate_site runs before site_to_env, so a bad value must come back as
    a listed problem rather than a traceback from the middle of derivation."""
    site["scale"]["expected_devices"] = "many"
    problems = inst.validate_site(site)
    assert any("whole number" in p for p in problems)


def test_a_bad_boolean_is_listed_with_the_other_problems(inst, site):
    site["ai_platform"]["allow_write"] = "sometimes"
    assert any("expected true or false" in p for p in inst.validate_site(site))


def test_port_overrides_accept_strings_or_ints(inst, site):
    site["ports"]["overrides"] = {"syslog": "514"}
    assert inst.site_to_env(site)["SYSLOG_PORT"] == "514"
