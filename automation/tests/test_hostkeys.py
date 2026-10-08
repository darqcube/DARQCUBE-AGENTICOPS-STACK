"""SSH host keys with Infrahub as the source of truth — offline.

Keys are built here, not generated with ssh-keygen, so the tests need no
OpenSSH and no device.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import os
import struct
import types
from pathlib import Path

import pytest
import yaml

from automation import hostkeys

ROOT = Path(__file__).resolve().parents[2]


def key(kind: str = "ssh-ed25519", seed: int = 1) -> tuple[str, str]:
    """A well-formed public key blob: the type string, then key material."""
    name = kind.encode()
    blob = struct.pack(">I", len(name)) + name + struct.pack(">I", 32) + bytes([seed]) * 32
    return kind, base64.b64encode(blob).decode()


def hashed(name: str) -> str:
    salt = os.urandom(20)
    mac = hmac.new(salt, name.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(mac).decode()}"


# --- the value stored in Infrahub ------------------------------------------

def test_keys_round_trip_through_the_infrahub_value():
    keys = [key(), key("ssh-rsa", 2)]
    assert hostkeys.parse(hostkeys.render(keys)) == keys


def test_empty_value_means_no_pinned_keys():
    assert hostkeys.parse(None) == [] and hostkeys.parse("  \n# note\n") == []


@pytest.mark.parametrize("text", [
    "ssh-dss AAAA",                                  # type not accepted
    "ssh-ed25519",                                   # no key
    "ssh-ed25519 not-base64!",                       # not base64
    f"ssh-rsa {key('ssh-ed25519')[1]}",              # label disagrees with the key
])
def test_a_malformed_line_is_an_error_not_skipped(text):
    """A silently dropped line changes which keys a device is trusted with."""
    with pytest.raises(hostkeys.HostKeyError):
        hostkeys.parse(text)


def test_fingerprint_matches_openssh_format():
    kind, blob = key()
    expected = base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode()
    assert hostkeys.fingerprint((kind, blob)) == "SHA256:" + expected.rstrip("=")


# --- what ssh learned on first connect --------------------------------------

def test_learned_keys_are_found_by_device_name_even_hashed(tmp_path):
    """Debian hashes known_hosts names; the learned file must still be read."""
    kh = tmp_path / "known_hosts"
    a, b = key(seed=1), key(seed=2)
    kh.write_text(f"{hashed('cr2')} {a[0]} {a[1]}\ncr1 {b[0]} {b[1]}\n")
    assert hostkeys.learned("cr2", kh) == [a]
    assert hostkeys.learned("cr1", kh) == [b]
    assert hostkeys.learned("dr1", kh) == []
    assert hostkeys.learned("dr1", tmp_path / "missing") == []


# --- deciding whether to pin -------------------------------------------------

def test_first_sight_is_pinned_and_says_to_verify():
    action, reason = hostkeys.decide("cr2", [key()], [], [], replace=False)
    assert action == "pin" and "verify" in reason


def test_a_key_matching_the_learned_one_is_pinned():
    action, reason = hostkeys.decide("cr2", [key()], [], [key()], replace=False)
    assert action == "pin" and "learned" in reason


def test_a_key_differing_from_infrahub_is_refused():
    """The case pinning exists for: an impostor, or an unannounced re-key."""
    action, reason = hostkeys.decide("cr2", [key(seed=9)], [key(seed=1)], [], replace=False)
    assert action == "refused" and "Infrahub" in reason and "--replace" in reason


def test_a_key_differing_from_the_learned_one_is_refused():
    action, _ = hostkeys.decide("cr2", [key(seed=9)], [], [key(seed=1)], replace=False)
    assert action == "refused"


def test_replace_accepts_a_verified_re_key():
    action, _ = hostkeys.decide("cr2", [key(seed=9)], [key(seed=1)], [key(seed=1)], replace=True)
    assert action == "pin"


def test_an_extra_key_type_is_not_a_conflict():
    """Pinned ed25519 only (from the YAML); the device also offers RSA."""
    action, _ = hostkeys.decide("cr2", [key(), key("ssh-rsa", 2)], [key()], [], replace=False)
    assert action == "pin"


def test_same_keys_are_unchanged_and_unreachable_fails():
    assert hostkeys.decide("cr2", [key()], [key()], [], False)[0] == "unchanged"
    assert hostkeys.decide("cr2", [], [], [], False)[0] == "failed"


# --- the files each SSH client checks against --------------------------------

def test_trust_file_holds_exactly_the_pinned_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(hostkeys, "TRUST_DIR", tmp_path / "trust")
    keys = [key(), key("ssh-rsa", 2)]
    path = hostkeys.trust_file("cr2.ssh", keys, ["cr2"])
    assert path.read_text() == "".join(f"cr2 {k} {b}\n" for k, b in keys)
    assert (tmp_path / "trust").stat().st_mode & 0o777 == 0o700
    assert hostkeys.trust_file("cr2.ssh", [], ["cr2"]).read_text() == ""


def test_paramiko_looks_up_by_address_and_brackets_other_ports():
    assert hostkeys.paramiko_name("192.0.2.10") == "192.0.2.10"
    assert hostkeys.paramiko_name("192.0.2.10", 2222) == "[192.0.2.10]:2222"


def test_pyats_with_pinned_keys_checks_strictly_and_learns_nothing():
    from automation.pyats import testbed

    opts = testbed.ssh_options("cr2", "/tmp/darqcube-hostkeys/cr2.ssh")
    assert "StrictHostKeyChecking=yes" in opts and "accept-new" not in opts
    assert "UserKnownHostsFile=/tmp/darqcube-hostkeys/cr2.ssh" in opts
    assert testbed.KNOWN_HOSTS not in opts
    assert "GlobalKnownHostsFile=/dev/null" in opts


# --- Netmiko, via the inventory ----------------------------------------------

def _host(name, value, monkeypatch, tmp_path):
    pytest.importorskip("nornir.core.inventory")   # other tests stub a bare `nornir`
    tasks = pytest.importorskip("automation.nornir.tasks")
    monkeypatch.setattr(hostkeys, "TRUST_DIR", tmp_path)
    node = types.SimpleNamespace(ssh_host_keys=types.SimpleNamespace(value=value))
    host = types.SimpleNamespace(name=name, hostname="192.0.2.10",
                                 data={"InfrahubNode": node}, connection_options={})
    tasks._apply_host_keys(host)
    return host


def test_netmiko_checks_pinned_keys(monkeypatch, tmp_path):
    host = _host("cr2", hostkeys.render([key()]), monkeypatch, tmp_path)
    extras = host.connection_options["netmiko"].extras
    assert extras["ssh_strict"] is True and extras["system_host_keys"] is False
    assert Path(extras["alt_key_file"]).read_text().startswith("192.0.2.10 ssh-ed25519 ")


def test_netmiko_unchanged_when_nothing_is_pinned(monkeypatch, tmp_path):
    host = _host("cr2", None, monkeypatch, tmp_path)
    assert host.connection_options == {} and host.data["ssh_host_keys"] == []


def test_a_broken_pin_fails_closed(monkeypatch, tmp_path):
    """No keys trusted, not 'any key accepted'."""
    host = _host("cr2", "ssh-ed25519 garbage!", monkeypatch, tmp_path)
    assert host.data["ssh_host_keys_error"]
    extras = host.connection_options["netmiko"].extras
    assert extras["ssh_strict"] is True and Path(extras["alt_key_file"]).read_text() == ""


# --- the schema and make seed -----------------------------------------------

def test_schema_has_an_optional_ssh_host_keys_attribute():
    schema = yaml.safe_load((ROOT / "source-of-truth/schema/darqcube.yml").read_text())
    device = next(n for n in schema["nodes"] if n["name"] == "Device")
    attr = next(a for a in device["attributes"] if a["name"] == "ssh_host_keys")
    assert attr["optional"] is True and len(attr["description"]) < 128


@pytest.fixture
def seed():
    spec = importlib.util.spec_from_file_location("seed_hk", ROOT / "source-of-truth/scripts/seed.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:
        pytest.skip(f"seed.py needs {exc.name}")
    return mod


def _seed_device(seed, rec):
    node = types.SimpleNamespace(
        role=types.SimpleNamespace(value="core"),
        ssh_host_keys=types.SimpleNamespace(value="ssh-ed25519 PINNED"),
        save=lambda: None,
    )
    views = {kind: {"attributes": {}, "relationships": {}} for _, kind in seed.SECTIONS}
    views["NetworkDevice"]["attributes"] = {
        "name": {"default": None}, "role": {"default": None}, "ssh_host_keys": {"default": None},
    }
    sections = {s: [] for s, _ in seed.SECTIONS}
    sections["devices"] = [("f.yml", rec)]
    existing = {kind: {} for _, kind in seed.SECTIONS}
    existing["NetworkDevice"]["cr2"] = node
    seed.apply(None, sections, views, existing, {})
    return node


def test_seed_keeps_pinned_keys_the_yaml_leaves_out(seed):
    """Otherwise every `make seed` would silently un-pin the fleet."""
    node = _seed_device(seed, {"name": "cr2", "role": "edge"})
    assert node.ssh_host_keys.value == "ssh-ed25519 PINNED"
    assert node.role.value == "edge"


def test_keys_set_in_the_yaml_still_win(seed):
    node = _seed_device(seed, {"name": "cr2", "role": "edge", "ssh_host_keys": "ssh-ed25519 NEW"})
    assert node.ssh_host_keys.value == "ssh-ed25519 NEW"
