"""Offline tests for seed.py.

The field list is read from the schema, so these build the same view seed.py
would get from Infrahub — but from darqcube.yml on disk. That makes the
checked-in example data a test of the real schema, not of a hand-kept copy.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "source-of-truth/schema/darqcube.yml"
PLATFORMS = yaml.safe_load((ROOT / "platforms.yml").read_text())


@pytest.fixture
def seed(monkeypatch):
    sdk = types.ModuleType("infrahub_sdk")
    sdk.InfrahubClientSync = object
    sdk.Config = lambda **kw: None
    monkeypatch.setitem(sys.modules, "infrahub_sdk", sdk)
    spec = importlib.util.spec_from_file_location("seed", ROOT / "source-of-truth/scripts/seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def views_from_schema_file() -> dict:
    """What schema_view() returns once darqcube.yml is loaded into Infrahub."""
    schema = yaml.safe_load(SCHEMA.read_text())
    views = {}
    for node in schema["nodes"]:
        attributes = {
            a["name"]: {
                "kind": a["kind"],
                "optional": a.get("optional", False),
                "default": a.get("default_value"),
                "choices": [c["name"] for c in a.get("choices", [])],
            }
            for a in node["attributes"]
        }
        relationships = {
            r["name"]: {"peer": r["peer"], "cardinality": r["cardinality"], "optional": r.get("optional", True)}
            for r in node.get("relationships", [])
            if r.get("kind", "Generic") in {"Attribute", "Generic"}
        }
        views[node["namespace"] + node["name"]] = {"attributes": attributes, "relationships": relationships}
    # Infrahub's built-in tag node.
    views["BuiltinTag"] = {
        "attributes": {
            "name": {"kind": "Text", "optional": False, "default": None, "choices": []},
            "description": {"kind": "Text", "optional": True, "default": None, "choices": []},
        },
        "relationships": {},
    }
    return views


def known_from(sections, seed) -> dict:
    known = {kind: set() for _, kind in seed.SECTIONS}
    for section, kind in seed.SECTIONS:
        known[kind] |= {rec["name"] for _, rec in sections[section]}
    return known


def check(seed, tmp_path, *docs: dict) -> list[str]:
    for n, doc in enumerate(docs):
        (tmp_path / f"f{n}.yml").write_text(yaml.safe_dump(doc))
    sections, errors = seed.load_records(tmp_path)
    if errors:
        return errors
    return seed.validate(sections, views_from_schema_file(), known_from(sections, seed), PLATFORMS)


SITE = {"name": "hq", "site_type": "hq"}
DEVICE = {"name": "cr1", "site": "hq", "role": "core", "platform": "ios_xe", "management_ip": "10.0.0.11"}


def test_checked_in_examples_are_valid(seed):
    sections, errors = seed.load_records(ROOT / "source-of-truth/devices")
    assert not errors
    assert seed.validate(sections, views_from_schema_file(), known_from(sections, seed), PLATFORMS) == []


def test_containerlab_mapping_from_the_admin_guide_is_valid(seed, tmp_path):
    """The lab in docs/administration/infrahub-guide.md, as YAML."""
    sites = [
        {"name": "nbp-hq", "site_type": "hq"},
        {"name": "nbp-branch-north", "site_type": "branch"},
        {"name": "telco", "site_type": "pop"},
    ]
    roles = {"cr1": "core-wan", "cs1": "core-dc", "ir1": "internet-edge", "b-north": "branch-wan", "telco1": "wan"}
    site_of = {"b-north": "nbp-branch-north", "telco1": "telco"}
    devices = [
        {"name": n, "site": site_of.get(n, "nbp-hq"), "role": r, "platform": "ios_xe",
         "management_host": f"clab-nbp-wan-clab-{n}.orb.local", "environment": "demo",
         "tags": ["containerlab", "nbp-wan"]}
        for n, r in roles.items()
    ]
    tags = [{"name": "containerlab"}, {"name": "nbp-wan"}]
    assert check(seed, tmp_path, {"tags": tags, "sites": sites, "devices": devices}) == []


def test_unknown_field_is_an_error_not_silently_dropped(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [{**DEVICE, "enviroment": "demo"}]})
    assert any("unknown field 'enviroment'" in e for e in errors)


def test_new_schema_field_needs_no_code_change(seed, tmp_path):
    """environment is not named anywhere in seed.py — the schema admits it."""
    assert "environment" not in (ROOT / "source-of-truth/scripts/seed.py").read_text()
    assert check(seed, tmp_path, {"sites": [SITE], "devices": [{**DEVICE, "environment": "demo"}]}) == []


def test_dropdown_value_outside_choices_is_rejected(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [{**DEVICE, "environment": "prod"}]})
    assert any("'prod' is not one of" in e for e in errors)


def test_unknown_site_and_tag_are_rejected(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [{**DEVICE, "site": "nowhere", "tags": ["x"]}]})
    assert any("site 'nowhere' does not exist" in e for e in errors)
    assert any("tags 'x' does not exist" in e for e in errors)


def test_unknown_platform_is_rejected(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [{**DEVICE, "platform": "ios_xr"}]})
    assert any("not in platforms.yml" in e for e in errors)


def test_device_needs_an_address(seed, tmp_path):
    no_address = {k: v for k, v in DEVICE.items() if k != "management_ip"}
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [no_address]})
    assert any("needs management_ip or management_host" in e for e in errors)


def test_fqdn_alone_is_enough(seed, tmp_path):
    fqdn_only = {**{k: v for k, v in DEVICE.items() if k != "management_ip"}, "management_host": "cr1.lab"}
    assert check(seed, tmp_path, {"sites": [SITE], "devices": [fqdn_only]}) == []


def test_bad_values_are_caught_before_any_write(seed, tmp_path):
    bad = {**DEVICE, "management_ip": "cr1.lab", "flow_enabled": "yes", "tags": "nbp-wan"}
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [bad]})
    assert any("is not an IP address" in e for e in errors)
    assert any("flow_enabled" in e for e in errors)
    assert any("tags must be a list" in e for e in errors)


def test_duplicate_name_across_files_is_rejected(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "devices": [DEVICE]}, {"devices": [DEVICE]})
    assert any("also defined in" in e for e in errors)


def test_unknown_top_level_key_is_rejected(seed, tmp_path):
    errors = check(seed, tmp_path, {"sites": [SITE], "device": [DEVICE]})
    assert any("unknown top-level key 'device'" in e for e in errors)
