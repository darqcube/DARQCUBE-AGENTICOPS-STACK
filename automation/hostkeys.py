"""SSH host keys, with Infrahub as the source of truth.

A device's public host keys live on its Infrahub node (`ssh_host_keys`, one
"<type> <base64>" per line). When they are set, every SSH session to the
device — pyATS (OpenSSH) and Netmiko (paramiko) — verifies the key against
them and refuses anything else. Nothing a device presents is ever written back
on its own: keys get into Infrahub only by `make pin-host-keys` (an operator
action) or from the device YAML (keys provisioned out of band — the strongest).

A device with no keys in Infrahub keeps the learn-once behaviour: pyATS files
its key by device name on first connect (automation/pyats/testbed.py).

Trust files are generated from Infrahub on every connection and never edited,
so a corrected key in Infrahub takes effect at once and a stale one cannot
linger in a cache.

    python -m automation.hostkeys pin [DEVICE ...] [--replace]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
# Generated per connection, so a tmpfs is the right place: never a source.
TRUST_DIR = Path(os.environ.get("HOSTKEYS_DIR", tempfile.gettempdir())) / "darqcube-hostkeys"

Key = tuple[str, str]   # (type, base64 blob)


class HostKeyError(ValueError):
    pass


def parse(text: str | None) -> list[Key]:
    """Infrahub's `ssh_host_keys` value -> [(type, blob)]. Empty -> [].

    Strict: a malformed line raises rather than being skipped, because a
    silently dropped key changes which keys a device is trusted with.
    """
    keys: list[Key] = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in KEY_TYPES:
            raise HostKeyError(f"line {n}: expected '<type> <base64>' with type one of "
                               f"{', '.join(KEY_TYPES)}")
        kind, blob = parts[0], parts[1]
        try:
            raw = base64.b64decode(blob, validate=True)
            (length,) = struct.unpack(">I", raw[:4])
            embedded = raw[4:4 + length].decode()
        except (binascii.Error, struct.error, UnicodeDecodeError) as exc:
            raise HostKeyError(f"line {n}: not a valid {kind} key ({exc})") from None
        if embedded != kind:
            raise HostKeyError(f"line {n}: says {kind} but the key is {embedded}")
        if (kind, blob) not in keys:
            keys.append((kind, blob))
    return keys


def render(keys: list[Key]) -> str:
    """[(type, blob)] -> the text stored in Infrahub."""
    return "".join(f"{kind} {blob}\n" for kind, blob in keys)


def fingerprint(key: Key) -> str:
    """SHA256 fingerprint, as `ssh-keygen -lf` and a device's CLI print it."""
    digest = hashlib.sha256(base64.b64decode(key[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def trust_file(label: str, keys: list[Key], names: list[str]) -> Path:
    """Write a known_hosts file holding `keys` under each of `names`; return it.

    One file per device and SSH client (`label`), replaced atomically:
    concurrent sessions to different devices never share a file, and a reader
    never sees half of one.
    """
    TRUST_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = "".join(f"{name} {kind} {blob}\n" for name in names for kind, blob in keys)
    fd, tmp = tempfile.mkstemp(dir=TRUST_DIR, prefix=f".{label}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    path = TRUST_DIR / label
    os.replace(tmp, path)
    return path


def paramiko_name(address: str, port: int = 22) -> str:
    """How paramiko looks a host up: the name dialled, bracketed off port 22."""
    return address if port == 22 else f"[{address}]:{port}"


# --- what is already known --------------------------------------------------

def _matches(field: str, name: str) -> bool:
    """Does a known_hosts host field (plain or |1|salt|hash) name `name`?"""
    for entry in field.split(","):
        if entry.startswith("|1|"):
            try:
                _, _, salt, digest = entry.split("|")
                mac = hmac.new(base64.b64decode(salt), name.encode(), hashlib.sha1).digest()
            except (ValueError, binascii.Error):
                continue
            if hmac.compare_digest(base64.b64encode(mac).decode(), digest):
                return True
        elif entry == name:
            return True
    return False


def learned(device: str, known_hosts: str | Path) -> list[Key]:
    """Keys ssh learned for `device` (filed by HostKeyAlias) in a known_hosts file."""
    try:
        lines = Path(known_hosts).read_text().splitlines()
    except FileNotFoundError:
        return []
    keys = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and not line.startswith(("#", "@")) and _matches(parts[0], device):
            keys.append((parts[1], parts[2]))
    return keys


def conflicts(scanned: list[Key], trusted: list[Key]) -> list[str]:
    """Key types present in both lists whose keys differ."""
    have = dict(trusted)
    return [kind for kind, blob in scanned if kind in have and have[kind] != blob]


def scan(address: str, port: int = 22, timeout: int = 5) -> list[Key]:
    """The host keys a device presents now (ssh-keyscan)."""
    proc = subprocess.run(
        ["ssh-keyscan", "-T", str(timeout), "-p", str(port), "-t",
         "ed25519,ecdsa,rsa", address],
        capture_output=True, text=True, timeout=timeout * 4,
    )
    keys = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1] in KEY_TYPES:
            keys.append((parts[1], parts[2]))
    return keys


# --- pinning ----------------------------------------------------------------

def decide(device: str, scanned: list[Key], pinned: list[Key], seen: list[Key],
           replace: bool) -> tuple[str, str]:
    """(action, reason) for one device. Pure, so every branch is testable."""
    if not scanned:
        return "failed", "no host key answered (unreachable, or SSH closed)"
    if not replace:
        if bad := conflicts(scanned, pinned):
            return "refused", (f"{', '.join(bad)} differs from the key pinned in Infrahub. "
                               f"Check the device's fingerprint on its console; if it was "
                               f"re-keyed on purpose, re-run with --replace")
        if bad := conflicts(scanned, seen):
            return "refused", (f"{', '.join(bad)} differs from the key learned on first "
                               f"connect. Verify on the console, then --replace")
    if sorted(scanned) == sorted(pinned):
        return "unchanged", "Infrahub already holds these keys"
    return "pin", "verified against the learned key" if seen else "first sight — verify the fingerprint"


def pin(devices: list[str] | None, replace: bool = False) -> int:
    from infrahub_sdk import Config, InfrahubClientSync

    from automation.nornir import tasks
    from automation.pyats import testbed

    nr = tasks.get_nornir(fresh=True)
    names = devices or sorted(nr.inventory.hosts)
    unknown = [d for d in names if d not in nr.inventory.hosts]
    if unknown:
        print(f"not in Infrahub: {', '.join(unknown)}", file=sys.stderr)
        return 2

    client = InfrahubClientSync(config=Config(address=tasks.INFRAHUB_URL,
                                              api_token=tasks.INFRAHUB_TOKEN))
    failures = 0
    for device in names:
        host = nr.inventory.hosts[device]
        scanned = scan(str(host.hostname))
        action, reason = decide(device, scanned, host.data.get("ssh_host_keys") or [],
                                learned(device, testbed.KNOWN_HOSTS), replace)
        print(f"  {device:<24}{action:<10}{reason}")
        if action == "pin":
            node = client.get(kind="NetworkDevice", name__value=device, branch=tasks.BRANCH)
            node.ssh_host_keys.value = render(scanned)
            node.save()
        if action in ("pin", "unchanged"):
            for key in scanned:
                print(f"  {'':<24}{key[0]:<20}{fingerprint(key)}")
        else:
            failures += 1
    tasks.invalidate_inventory()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m automation.hostkeys")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pin", help="scan devices and pin their SSH host keys in Infrahub")
    p.add_argument("devices", nargs="*", help="default: every device in Infrahub")
    p.add_argument("--replace", action="store_true",
                   help="accept keys that differ from what is pinned or learned")
    args = parser.parse_args(argv)
    return pin(args.devices, args.replace)


if __name__ == "__main__":
    sys.exit(main())
