#!/usr/bin/env python3
"""DARQCUBE-AGENTICOPS-STACK installer.

    python3 install.py

One file, standard library only — it has to run on a freshly cloned repo before
anything is set up, so it cannot depend on pip having been used yet.

Seven steps, each resumable. Re-running is safe: nothing is overwritten that
already holds real values.

    1. preflight     OS, Docker, Compose, RAM, disk, ports, kernel tuning
    2. configure     .env from .env.example, secrets generated
    3. build         the two images built from source
    4. start         docker compose up -d --wait
    5. initialise    schema, seed, render
    6. verify        pytest — containers, services, and the wiring between them
    7. report        where everything is and what to do next

Common options:
    --check          run preflight only, change nothing
    --yes            never prompt; fail instead of asking
    --fix-sysctl     apply the kernel settings and stop (needs sudo)
    --skip-tests     stop after step 5
    --step N         run one step only
    --ai-platform F  connect an ai-platform on another host: apply the file
                     scripts/ai-platform-connect.py generated there
                     (default: sites/ai-platform.yml, if present)
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
SITE = ROOT / "site.yml"
SITE_EXAMPLE = ROOT / "site.example.yml"
# Generated on the ai-platform host by scripts/ai-platform-connect.py and copied
# here. Gitignored (sites/*.yml): it names this deployment's addresses.
AI_PLATFORM_OVERLAY = ROOT / "sites" / "ai-platform.yml"
VENV = ROOT / ".venv"

# Secrets that only need to be unguessable. Each gets its own value: one string
# reused everywhere means a leak anywhere is a leak everywhere.
GENERATED = [
    "NEO4J_PASSWORD", "RABBITMQ_PASSWORD", "POSTGRES_PASSWORD",
    "INFRAHUB_SECRET_KEY", "INFRAHUB_ADMIN_TOKEN", "MCP_AUTH_TOKEN",
    "GRAFANA_ADMIN_PASSWORD", "RENDER_HOOK_SECRET",
]

# Values only the operator can know.
REQUIRED = {
    "DEVICE_USER": "SSH username for your network devices",
    "DEVICE_PASSWORD": "SSH password for that account",
    "SNMPV3_AUTH": "SNMPv3 authentication passphrase (must match the devices)",
    "SNMPV3_PRIV": "SNMPv3 privacy passphrase (must match the devices)",
    "SYSLOG_COLLECTOR_IP": "this machine's routable IP — devices send syslog and flow here",
}

# UDP receive buffers. Devices push syslog and flow over UDP, which has no
# retransmit: an undersized socket buffer drops datagrams silently. The kernel
# clamps whatever the application asks for to these ceilings, and stock Ubuntu
# ships 212992 (208 KiB) against the 8 MiB Logstash and Telegraf request.
SYSCTLS = {
    "net.core.rmem_max": 8_388_608,
    "net.core.rmem_default": 1_048_576,
}

C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[1m", "d": "\033[2m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = dict.fromkeys(C, "")

_failed = False


def head(n: int, title: str) -> None:
    print(f"\n{C['b']}[{n}/7] {title}{C['x']}")


def ok(msg: str) -> None:
    print(f"  {C['g']}ok{C['x']}    {msg}")


def bad(msg: str) -> None:
    global _failed
    _failed = True
    print(f"  {C['r']}FAIL{C['x']}  {msg}")


def warn(msg: str) -> None:
    print(f"  {C['y']}warn{C['x']}  {msg}")


def info(msg: str) -> None:
    print(f"  {C['d']}·{C['x']}     {msg}")


def run(cmd: list[str], check: bool = True, capture: bool = True, timeout: int = 600):
    """Run a command in the repo root."""
    return subprocess.run(
        cmd, cwd=ROOT, check=check, timeout=timeout,
        capture_output=capture, text=True,
    )


def stream(cmd: list[str], timeout: int = 1800) -> int:
    """Run a command showing its output — for builds, which are slow."""
    print(f"  {C['d']}$ {' '.join(cmd)}{C['x']}")
    return subprocess.run(cmd, cwd=ROOT, timeout=timeout).returncode


# ------------------------------------------------------------- the site file

class SiteError(Exception):
    """The site file is missing something, or says something impossible."""


def read_yaml(path: Path) -> dict:
    """A deliberately small YAML reader for our own site file.

    install.py must run on a freshly cloned repo before pip has been used, so
    it cannot import PyYAML. The site file is a format we control, so a reader
    for the subset it uses is a fair trade — and a test asserts this produces
    exactly what PyYAML would for site.example.yml, so the subset is proven
    rather than hoped for.

    Supports: comments, nested mappings by indentation, `- ` list items,
    quoted and bare scalars, int / bool / empty. Nothing else, on purpose.
    """
    root: dict = {}
    # (indent, container) — the innermost open mapping is always last.
    stack: list[tuple[int, dict]] = [(-1, root)]
    current_list: list | None = None
    list_indent = -1

    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        if line.startswith("- "):
            if current_list is None or indent < list_indent:
                raise SiteError(f"{path.name}:{lineno}: list item outside a list")
            current_list.append(_scalar(line[2:].strip()))
            continue

        current_list = None
        if ":" not in line:
            raise SiteError(f"{path.name}:{lineno}: expected 'key: value' — got {line!r}")

        key, _, value = line.partition(":")
        key, value = key.strip(), value.split(" #")[0].strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise SiteError(f"{path.name}:{lineno}: indentation does not match any parent")
        parent = stack[-1][1]

        if value == "":
            # Either a nested mapping or a list — decided by the next line.
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            current_list = []
            list_indent = indent
            parent[key] = child
            # Keep both possibilities open; _finalise picks the one that was used.
            child["__list__"] = current_list
        else:
            parent[key] = _scalar(value)

    return _finalise(root)


def _scalar(text: str):
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("", "null", "~"):
        return ""
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _finalise(node):
    """Collapse the list/mapping ambiguity left by read_yaml."""
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "__list__":
            continue
        if isinstance(value, dict):
            items = value.get("__list__")
            rest = {k: v for k, v in value.items() if k != "__list__"}
            out[key] = _finalise(rest) if rest else (items or {})
        else:
            out[key] = value
    return out


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def as_bool(value, field: str, default: bool = False) -> bool:
    """Interpret a boolean the way the author meant it.

    Quoting a boolean in YAML makes it a STRING, and a non-empty string is
    truthy — so `allow_write: "false"` would ENABLE write access to devices,
    the exact opposite of what was written, silently. Quotes are easy to add by
    habit and there is no way to notice the result.

    Accepts real booleans and the usual spellings; anything else raises rather
    than guessing.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise SiteError(
        f"{field}: expected true or false, got {value!r}. "
        f"Accepted: true/false, yes/no, on/off."
    )


def as_int(value, field: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        raise SiteError(f"{field}: expected a whole number, got {value!r}") from None


def dig(site: dict, path: str, default=None):
    node = site
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# expected_devices -> the settings you would otherwise derive from docs/scale.md
SCALE_BANDS = [
    #  up to, interval, shard, concurrency
    (50,   "30s", 150,  8),
    (400,  "60s", 150, 16),
    (10_000, "120s", 200, 24),
]

STANDARD_PORTS = {
    "grafana": 3000, "infrahub": 8000, "prometheus": 9090, "alertmanager": 9093,
    "loki": 3100, "automation": 8100, "syslog": 514, "netflow": 2055, "ipfix": 4739,
    "flow_dedicated_first": 2056, "flow_dedicated_last": 2105,
}
OFFSET_PORTS = {
    "grafana": 13000, "infrahub": 18000, "prometheus": 19090, "alertmanager": 19093,
    "loki": 13100, "automation": 18100, "syslog": 1514, "netflow": 12055, "ipfix": 14739,
    "flow_dedicated_first": 12056, "flow_dedicated_last": 12105,
}
PORT_VARS = {
    "grafana": "GRAFANA_PORT", "infrahub": "INFRAHUB_PORT", "prometheus": "PROMETHEUS_PORT",
    "alertmanager": "ALERTMANAGER_PORT", "loki": "LOKI_PORT", "automation": "AUTOMATION_PORT",
    "syslog": "SYSLOG_PORT", "netflow": "NETFLOW_PORT", "ipfix": "IPFIX_PORT",
    "flow_dedicated_first": "FLOW_DEDICATED_FIRST", "flow_dedicated_last": "FLOW_DEDICATED_LAST",
}


AUTH_MODES = ("token", "oidc", "both")
AI_PLATFORM_KEYS = {
    "": {"enabled", "allow_write", "publish", "auth", "max_response_kb"},
    "publish": {"enabled", "bind_ip"},
    "auth": {"mode", "issuer", "jwks_url", "audiences", "write_role"},
}


def ai_platform_env(site: dict, collector_ip: str, grafana_port: str) -> dict[str, str]:
    """The MCP settings an ai-platform on another machine needs.

    Separate from site_to_env because it is also applied to an EXISTING .env
    (an install done without a site file): only these keys change there.
    """
    env: dict[str, str] = {}
    publish = as_bool(dig(site, "ai_platform.publish.enabled"), "ai_platform.publish.enabled")
    # Empty bind_ip means the address devices already use for this host.
    bind = str(dig(site, "ai_platform.publish.bind_ip", "") or "").strip() or collector_ip
    env["MCP_BIND_IP"] = bind if publish else "127.0.0.1"
    # The MCP SDK answers only Host headers it was told about (421 otherwise).
    env["MCP_ALLOWED_HOSTS"] = f"{bind}:*" if publish else ""

    env["MCP_AUTH_MODE"] = str(dig(site, "ai_platform.auth.mode", "token") or "token").strip().lower()
    env["MCP_OIDC_ISSUER"] = str(dig(site, "ai_platform.auth.issuer", "") or "").strip()
    env["MCP_OIDC_JWKS_URL"] = str(dig(site, "ai_platform.auth.jwks_url", "") or "").strip()
    audiences = dig(site, "ai_platform.auth.audiences", "") or ""
    if isinstance(audiences, list):
        audiences = ",".join(str(a).strip() for a in audiences if str(a).strip())
    env["MCP_OIDC_AUDIENCES"] = str(audiences).strip()
    env["MCP_WRITE_ROLE"] = str(dig(site, "ai_platform.auth.write_role", "") or "darqcube-write").strip()
    env["MCP_MAX_RESPONSE_BYTES"] = str(
        as_int(dig(site, "ai_platform.max_response_kb"), "ai_platform.max_response_kb", 512) * 1024
    )
    # Links mcp-grafana hands a person must open in a browser, not resolve only
    # inside the compose network.
    known_ip = collector_ip and collector_ip != "CHANGEME"
    env["GRAFANA_PUBLIC_URL"] = f"http://{collector_ip}:{grafana_port}" if known_ip else ""
    return env


def validate_ai_platform(site: dict) -> list[str]:
    """Problems in the ai_platform block — from site.yml or the overlay."""
    problems: list[str] = []
    block = dig(site, "ai_platform", {}) or {}
    if not isinstance(block, dict):
        return ["ai_platform must be a mapping"]
    for section, allowed in AI_PLATFORM_KEYS.items():
        node = block if not section else (block.get(section) or {})
        if not isinstance(node, dict):
            problems.append(f"ai_platform.{section} must be a mapping")
            continue
        for key in node:
            if key not in allowed:
                where = f"ai_platform.{section}" if section else "ai_platform"
                problems.append(f"unknown key '{key}' in {where} — expected one of {sorted(allowed)}")

    try:
        publish = as_bool(dig(site, "ai_platform.publish.enabled"), "ai_platform.publish.enabled")
    except SiteError as exc:
        problems.append(str(exc))
        publish = False
    bind = str(dig(site, "ai_platform.publish.bind_ip", "") or "").strip()
    if publish and bind and bind != "CHANGEME":
        try:
            if ipaddress.ip_address(bind).is_loopback:
                problems.append(f"ai_platform.publish.bind_ip {bind} is loopback — "
                                f"another machine cannot reach it; leave it empty to use site.collector_ip")
            else:
                local = local_addresses()
                if local and bind not in local:
                    problems.append(f"ai_platform.publish.bind_ip {bind} is not an address on this machine "
                                    f"(found: {', '.join(sorted(local))})")
        except ValueError:
            problems.append(f"ai_platform.publish.bind_ip '{bind}' is not an IP address")
    elif publish and bind == "CHANGEME":
        problems.append("ai_platform.publish.bind_ip is CHANGEME — set this machine's address, "
                        "or leave it empty to use site.collector_ip")

    mode = str(dig(site, "ai_platform.auth.mode", "token") or "token").strip().lower()
    if mode not in AUTH_MODES:
        problems.append(f"ai_platform.auth.mode '{mode}' — expected one of {', '.join(AUTH_MODES)}")
    elif mode in ("oidc", "both"):
        for field in ("issuer", "jwks_url"):
            value = str(dig(site, f"ai_platform.auth.{field}", "") or "")
            if not re.match(r"^https?://\S+$", value):
                problems.append(
                    f"ai_platform.auth.{field} is required for mode {mode} — an http(s) URL "
                    f"(generate it on the ai-platform host: scripts/ai-platform-connect.py)"
                )

    try:
        kb = as_int(dig(site, "ai_platform.max_response_kb"), "ai_platform.max_response_kb", 512)
        if not 16 <= kb <= 4096:
            problems.append(f"ai_platform.max_response_kb {kb} — expected 16 to 4096")
    except SiteError as exc:
        problems.append(str(exc))
    return problems


def merge_overlay(site: dict, overlay: dict, name: str) -> dict:
    """Deep-merge an ai-platform overlay over the site file.

    The overlay may only speak for `ai_platform` — it is generated on another
    machine, and must not be able to change device credentials or ports.
    """
    extra = set(overlay) - {"ai_platform"}
    if extra:
        raise SiteError(f"{name} may only contain 'ai_platform' — found {sorted(extra)}")

    def merge(base: dict, top: dict) -> dict:
        out = dict(base)
        for key, value in top.items():
            out[key] = merge(out.get(key) or {}, value) if isinstance(value, dict) else value
        return out

    return merge(site, overlay)


def set_env_values(text: str, values: dict[str, str]) -> str:
    """Set KEY=value lines in .env text, appending any that are missing.

    An .env from an older .env.example lacks the newer keys; appending them is
    what lets an existing install take an ai-platform overlay.
    """
    missing = []
    for key, value in values.items():
        pattern = re.compile(rf"^{key}=.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _m, k=key, v=value: f"{k}={v}", text)
        else:
            missing.append(f"{key}={value}")
    if missing:
        text = text.rstrip("\n") + "\n\n# --- ai-platform on another host (added by install.py) ---\n"
        text += "\n".join(missing) + "\n"
    return text


def site_to_env(site: dict) -> dict[str, str]:
    """Site file -> the .env variables it determines.

    Only the keys the site file speaks for. Everything else in .env.example
    keeps its default, and the eight secrets are generated separately.
    """
    env: dict[str, str] = {}

    env["DEPLOYMENT_NAME"] = str(dig(site, "site.name", "darqcube"))
    env["SYSLOG_COLLECTOR_IP"] = str(dig(site, "site.collector_ip", ""))

    env["DEVICE_USER"] = str(dig(site, "devices.ssh_user", ""))
    env["DEVICE_PASSWORD"] = str(dig(site, "devices.ssh_password", ""))
    env["SNMPV3_USER"] = str(dig(site, "devices.snmpv3.user", "darqcube"))
    env["SNMPV3_AUTH"] = str(dig(site, "devices.snmpv3.auth", ""))
    env["SNMPV3_PRIV"] = str(dig(site, "devices.snmpv3.priv", ""))
    env["GNMI_USER"] = str(dig(site, "devices.gnmi_user", "") or "")
    env["GNMI_PASSWORD"] = str(dig(site, "devices.gnmi_password", "") or "")

    # Derived, so nobody has to read docs/scale.md under time pressure.
    count = as_int(dig(site, "scale.expected_devices"), "scale.expected_devices", 50)
    for ceiling, interval, shard, concurrency in SCALE_BANDS:
        if count <= ceiling:
            env["SNMP_INTERVAL"] = interval
            env["SNMP_SHARD_SIZE"] = str(shard)
            env["AUTOMATION_CONCURRENCY"] = str(concurrency)
            break
    env["PROM_RETENTION"] = str(dig(site, "scale.metrics_retention", "15d"))
    env["LOKI_RETENTION"] = str(dig(site, "scale.logs_retention", "336h"))

    chosen = STANDARD_PORTS if as_bool(dig(site, "ports.standard"), "ports.standard") else OFFSET_PORTS
    overrides = dig(site, "ports.overrides", {}) or {}
    for name, var in PORT_VARS.items():
        env[var] = str(as_int(overrides.get(name), f"ports.overrides.{name}", chosen[name]))

    env["ALERT_WEBHOOK_URL"] = str(dig(site, "alerts.webhook_url", "") or "")

    profiles = ["devices", "automation"]
    if as_bool(dig(site, "ai_platform.enabled"), "ai_platform.enabled", default=True):
        profiles.append("mcp")
    if as_bool(dig(site, "source_of_truth.auto_render"), "source_of_truth.auto_render"):
        profiles.append("auto-render")
    env["COMPOSE_PROFILES"] = ",".join(profiles)
    env["MCP_ALLOW_WRITE"] = str(
        as_bool(dig(site, "ai_platform.allow_write"), "ai_platform.allow_write")
    ).lower()
    env.update(ai_platform_env(site, env["SYSLOG_COLLECTOR_IP"], env["GRAFANA_PORT"]))

    return env


def validate_site(site: dict) -> list[str]:
    """Everything worth catching before a single container starts."""
    problems: list[str] = []

    # Typos in keys are silent otherwise — the value is simply never applied.
    known = {"site", "devices", "scale", "ports", "alerts", "ai_platform", "source_of_truth"}
    for key in site:
        if key not in known:
            problems.append(f"unknown top-level key '{key}' — expected one of {sorted(known)}")

    name = dig(site, "site.name")
    if not name:
        problems.append("site.name is required")

    # The most common deployment mistake in this stack.
    raw_ip = str(dig(site, "site.collector_ip", "") or "")
    if not raw_ip or raw_ip == "CHANGEME":
        problems.append("site.collector_ip is required — this machine's routable IP")
    else:
        try:
            addr = ipaddress.ip_address(raw_ip)
            if addr.is_loopback:
                problems.append(
                    f"site.collector_ip {raw_ip} is a loopback address — devices "
                    f"cannot reach it. Use the address from: ip -4 addr show scope global"
                )
            else:
                # Gate on whether we could read the interfaces, not on the OS —
                # the fallback works anywhere ifconfig exists. An empty result
                # means we could not tell, which is not the same as wrong.
                local = local_addresses()
                if local and raw_ip not in local:
                    problems.append(
                        f"site.collector_ip {raw_ip} is not an address on this machine "
                        f"(found: {', '.join(sorted(local))}). Devices would send "
                        f"syslog and flow into a black hole."
                    )
        except ValueError:
            problems.append(f"site.collector_ip '{raw_ip}' is not an IPv4/IPv6 address")

    for subnet in dig(site, "site.device_subnets", []) or []:
        try:
            ipaddress.ip_network(str(subnet), strict=False)
        except ValueError:
            problems.append(f"site.device_subnets: '{subnet}' is not a valid CIDR")

    for field, label in (("devices.ssh_user", "SSH username"),
                         ("devices.ssh_password", "SSH password"),
                         ("devices.snmpv3.auth", "SNMPv3 auth passphrase"),
                         ("devices.snmpv3.priv", "SNMPv3 privacy passphrase")):
        value = str(dig(site, field, "") or "")
        if not value or value == "CHANGEME":
            problems.append(f"{field} is required — the {label}")

    for field, default in (("ports.standard", False),
                           ("ai_platform.enabled", True),
                           ("ai_platform.allow_write", False),
                           ("source_of_truth.auto_render", False)):
        try:
            as_bool(dig(site, field), field, default)
        except SiteError as exc:
            problems.append(str(exc))

    problems.extend(validate_ai_platform(site))

    try:
        count = as_int(dig(site, "scale.expected_devices"), "scale.expected_devices", 50)
    except SiteError as exc:
        problems.append(str(exc))
        count = 50
    if count < 1:
        problems.append("scale.expected_devices must be a positive whole number")
    else:
        if count > 400:
            problems.append(
                f"scale.expected_devices {count} is beyond the tested ceiling of 400. "
                f"It will still install — read docs/scale.md first."
            )
        try:
            mem_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
            if count > 150 and mem_gb < 16:
                problems.append(
                    f"{count} devices on {mem_gb:.0f} GB RAM — 24 GB is recommended "
                    f"at that size. Expect Prometheus and Neo4j to contend."
                )
        except (ValueError, OSError):
            pass

    return problems


def local_addresses() -> set[str]:
    """Every IPv4 address configured on this host."""
    found: set[str] = set()
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=10)
        found |= set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out.stdout))
    except Exception:
        pass
    if not found:
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=10)
            found |= set(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out.stdout))
        except Exception:
            pass
    return found


def check_site_is_not_committed() -> None:
    """The site file holds credentials. Refuse if it has been committed."""
    try:
        out = subprocess.run(["git", "ls-files", "--error-unmatch", SITE.name],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            bad(f"{SITE.name} is tracked by git and contains credentials. "
                f"Remove it from the index: git rm --cached {SITE.name}")
    except Exception:
        pass


# ---------------------------------------------------------------- 1. preflight

def read_env(path: Path = ENV) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.split("#")[0].strip()
    return values


def _has_venv() -> bool:
    """python3-venv is a separate apt package on Debian and Ubuntu, and step 6
    cannot run without it."""
    try:
        import ensurepip  # noqa: F401
        import venv  # noqa: F401
        return True
    except ImportError:
        return False


def port_free(port: int) -> bool:
    for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM)):
        s = socket.socket(family, kind)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def docker_unreachable(err: str) -> None:
    """Turn docker's own stderr into the fix for it."""
    first = next((l for l in err.splitlines() if l.strip()), "")
    low = err.lower()

    if "permission denied" in low and "docker.sock" in low:
        user = getpass.getuser()
        if _in_docker_group(user):
            bad(f"{user} is in the docker group, but this login session started before "
                f"that and carries the old group list. Fix: log out and back in.")
        else:
            bad(f"the docker daemon is running, but {user} is not allowed to reach "
                f"/var/run/docker.sock. Fix: sudo usermod -aG docker {user} — "
                f"then log out and back in.")
        # newgrp/sg come from the shadow utils, which minimal and container images
        # do not always ship. Only offer them if they are actually here, so the
        # advice never names a command this host does not have.
        shortcut = next((c for c in ("newgrp", "sg") if shutil.which(c)), None)
        if shortcut == "newgrp":
            info("to avoid reconnecting: newgrp docker")
        elif shortcut == "sg":
            info("to avoid reconnecting: sg docker -c bash")
        info("do not install as root — .env and the Docker volumes would end up root-owned")
        return

    if "cannot connect" in low or "is the docker daemon running" in low:
        bad(f"the docker daemon is not reachable: {first} "
            f"— try: sudo systemctl start docker")
        return

    bad(f"docker is installed but did not answer: {first}")


def _in_docker_group(user: str) -> bool:
    try:
        import grp  # Unix only, and this branch is only reached on a Unix socket error
        return user in grp.getgrnam("docker").gr_mem
    except Exception:
        return False


def current_sysctl(key: str) -> int | None:
    try:
        out = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def preflight(args) -> None:
    head(1, "Preflight")

    # --- OS ---
    if platform.system() != "Linux":
        warn(f"{platform.system()} — this stack targets Ubuntu Linux. Continuing, "
             f"but host metrics and UDP tuning will not behave the same.")
    else:
        try:
            rel = dict(
                l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l
            )
            ok(f"{rel.get('PRETTY_NAME', 'Linux').strip(chr(34))}")
        except Exception:
            ok("Linux")

    # --- host tooling ---
    # install.py itself needs only python3 and docker. The rest are for USING
    # the stack afterwards, and a minimal Ubuntu Server has none of them — far
    # better to say so now than to fail at `make state` a week later.
    if not _has_venv():
        bad("python3-venv is missing — step 6 cannot create a test environment. "
            "Fix: ./scripts/prepare-ubuntu.sh (or sudo apt install -y python3-venv)")
    else:
        ok("python3-venv")

    for tool, why in (("make", "every documented command (make up, make seed, make render)"),
                      ("jq", "make state / check / config-get pretty-print with it"),
                      ("git", "cloning and updating this repo")):
        if shutil.which(tool):
            ok(tool)
        else:
            warn(f"{tool} is not installed — needed for {why}. "
                 f"Fix: ./scripts/prepare-ubuntu.sh (or sudo apt install -y {tool})")

    # --- Docker ---
    if not shutil.which("docker"):
        bad("docker not found — Fix: ./scripts/prepare-ubuntu.sh (or see docs/install/01-prerequisites.md)")
        return
    try:
        proc = run(["docker", "version", "--format", "{{.Server.Version}}"], check=False, timeout=30)
    except Exception as exc:
        bad(f"docker is installed but could not be run: {exc}")
        return
    if proc.returncode == 0:
        ok(f"docker {proc.stdout.strip()}")
    else:
        # Docker already said what is wrong. Reporting a guess instead sends people
        # to restart a daemon that is running — the usual cause is socket
        # permissions, which systemctl cannot fix.
        docker_unreachable((proc.stderr or proc.stdout).strip())
        return

    try:
        cv = run(["docker", "compose", "version", "--short"]).stdout.strip()
    except Exception:
        bad("docker compose v2 plugin not found")
        return
    parts = re.findall(r"\d+", cv)
    major, minor = (int(parts[0]), int(parts[1])) if len(parts) >= 2 else (0, 0)
    # compose.yaml uses `include:`, added in v2.20.
    if (major, minor) >= (2, 20):
        ok(f"docker compose {cv}")
    else:
        bad(f"docker compose {cv} is too old — compose.yaml uses 'include:', needs v2.20+")

    # --- resources, sized for the documented 400-device ceiling ---
    try:
        mem_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError):
        mem_gb = 0
    cpus = os.cpu_count() or 0
    disk_gb = shutil.disk_usage(ROOT).free / 1024**3

    (ok if mem_gb >= 16 else warn)(f"{mem_gb:.0f} GB RAM" + ("" if mem_gb >= 16 else " — 24 GB recommended"))
    (ok if cpus >= 4 else warn)(f"{cpus} CPUs" + ("" if cpus >= 4 else " — 8 recommended"))
    (ok if disk_gb >= 50 else warn)(f"{disk_gb:.0f} GB free" + ("" if disk_gb >= 50 else " — 200 GB recommended"))

    check_sysctls(args)

    # --- ports ---
    env = read_env() or read_env(ENV_EXAMPLE)
    ports = {
        "Grafana": env.get("GRAFANA_PORT", "3000"), "Infrahub": env.get("INFRAHUB_PORT", "8000"),
        "Prometheus": env.get("PROMETHEUS_PORT", "9090"), "Alertmanager": env.get("ALERTMANAGER_PORT", "9093"),
        "Loki": env.get("LOKI_PORT", "3100"), "Automation": env.get("AUTOMATION_PORT", "8100"),
        "syslog": env.get("SYSLOG_PORT", "514"), "NetFlow": env.get("NETFLOW_PORT", "2055"),
        "IPFIX": env.get("IPFIX_PORT", "4739"),
    }
    busy = [f"{n}:{p}" for n, p in ports.items() if p.isdigit() and not port_free(int(p))]
    if busy:
        for entry in busy:
            name, port = entry.split(":")
            bad(f"port {port} ({name}) is in use — change it in .env, see docs/how-to/change-ports.md")
    else:
        ok("all configured ports are free")


def check_sysctls(args) -> None:
    """The single most consequential host setting: too small and syslog and flow
    are dropped by the kernel with no error, no log line and no retransmit."""
    if platform.system() != "Linux":
        info("kernel UDP buffer check skipped (not Linux)")
        return

    needs_fix = {}
    for key, want in SYSCTLS.items():
        have = current_sysctl(key)
        if have is None:
            warn(f"could not read {key}")
        elif have < want:
            needs_fix[key] = (have, want)
        else:
            ok(f"{key} = {have}")
    if not needs_fix:
        return

    # With --fix-sysctl these values are about to be corrected, so they are not
    # failures yet: apply_sysctl() reports the outcome and calls bad() only if one
    # did not take. Calling bad() here first would set the sticky _failed flag and
    # stop the run after a fix that worked.
    report = warn if args.fix_sysctl else bad
    for key, (have, want) in needs_fix.items():
        report(f"{key} = {have}, needs >= {want} — "
               f"{'fixing' if args.fix_sysctl else 'UDP syslog and flow will be dropped silently'}")

    if args.fix_sysctl:
        apply_sysctl(needs_fix)
        return

    print(f"\n  {C['y']}Fix with:{C['x']}  sudo python3 install.py --fix-sysctl")
    print(f"  {C['d']}or manually:{C['x']}")
    for key, (_, want) in needs_fix.items():
        print(f"    echo '{key} = {want}' | sudo tee -a /etc/sysctl.d/99-darqcube.conf")
    print("    sudo sysctl --system")


def apply_sysctl(needs_fix: dict) -> None:
    """Write the kernel settings so they survive a reboot, then apply them."""
    if os.geteuid() != 0:
        bad("--fix-sysctl needs root: sudo python3 install.py --fix-sysctl")
        return
    conf = Path("/etc/sysctl.d/99-darqcube.conf")
    lines = [
        "# DARQCUBE-AGENTICOPS-STACK — UDP receive buffers.",
        "# Devices push syslog and flow over UDP. An undersized socket buffer",
        "# drops datagrams silently: no error, no log line, no retransmit.",
    ]
    lines += [f"{k} = {v}" for k, v in SYSCTLS.items()]
    conf.write_text("\n".join(lines) + "\n")
    subprocess.run(["sysctl", "--system"], capture_output=True)
    for key, (_, want) in needs_fix.items():
        now = current_sysctl(key)
        (ok if now and now >= want else bad)(f"{key} = {now} (persisted in {conf})")


# ---------------------------------------------------------------- 2. configure

def configure(args) -> None:
    head(2, "Configure")

    site_path = Path(args.from_site) if args.from_site else (SITE if SITE.exists() else None)
    overlay = ai_platform_overlay_path(args)
    if site_path:
        configure_from_site(site_path, args, overlay)
        return

    info(f"no site file — using prompts. For a repeatable install, "
         f"cp {SITE_EXAMPLE.name} {SITE.name} and fill it in.")
    configure_interactive(args)
    if overlay:
        apply_ai_platform_to_env(overlay)


def ai_platform_overlay_path(args) -> Path | None:
    explicit = getattr(args, "ai_platform", None)
    if explicit:
        path = Path(explicit)
        if not path.exists():
            bad(f"{path} not found — generate it on the ai-platform host with "
                f"scripts/ai-platform-connect.py")
            return None
        return path
    return AI_PLATFORM_OVERLAY if AI_PLATFORM_OVERLAY.exists() else None


def read_overlay(path: Path) -> dict | None:
    try:
        overlay = read_yaml(path)
        merge_overlay({}, overlay, path.name)       # shape check only
    except SiteError as exc:
        bad(str(exc))
        return None
    return overlay


def apply_ai_platform_to_env(path: Path) -> None:
    """Apply an ai-platform overlay to an existing .env — an install done
    without a site file. Only the MCP keys (and GRAFANA_PUBLIC_URL) change."""
    overlay = read_overlay(path)
    if overlay is None:
        return
    problems = validate_ai_platform(overlay)
    if problems:
        for problem in problems:
            bad(f"{path.name}: {problem}")
        return
    current = read_env(ENV)
    values = ai_platform_env(overlay, current.get("SYSLOG_COLLECTOR_IP", ""),
                             current.get("GRAFANA_PORT", "3000"))
    if dig(overlay, "ai_platform.allow_write") is not None:
        values["MCP_ALLOW_WRITE"] = str(
            as_bool(dig(overlay, "ai_platform.allow_write"), "ai_platform.allow_write")).lower()
    ENV.write_text(set_env_values(ENV.read_text(), values))
    os.chmod(ENV, 0o600)
    report_ai_platform(values, path.name)


def report_ai_platform(values: dict[str, str], source: str) -> None:
    if values.get("MCP_BIND_IP", "127.0.0.1") == "127.0.0.1":
        info("MCP servers: this host only (ai_platform.publish is off)")
        return
    ok(f"ai-platform settings applied from {source}")
    info(f"MCP servers published on {values['MCP_BIND_IP']}:9001-9007 · "
         f"auth {values['MCP_AUTH_MODE']}")
    info("recreate them to take effect: make mcp-apply")


def configure_from_site(site_path: Path, args, overlay_path: Path | None = None) -> None:
    """Generate .env from the site file. Nothing else is touched."""
    if not site_path.exists():
        bad(f"{site_path} not found. Start from: cp {SITE_EXAMPLE.name} {site_path.name}")
        return

    check_site_is_not_committed()

    # It holds credentials; say so if the filesystem disagrees.
    mode = site_path.stat().st_mode & 0o077
    if mode:
        warn(f"{site_path.name} is readable by others — fix with: chmod 600 {site_path.name}")

    try:
        site = read_yaml(site_path)
    except SiteError as exc:
        bad(str(exc))
        return
    ok(f"read {site_path.name}")
    if overlay_path:
        overlay = read_overlay(overlay_path)
        if overlay is None:
            return
        site = merge_overlay(site, overlay, overlay_path.name)
        ok(f"merged {overlay_path.relative_to(ROOT) if overlay_path.is_relative_to(ROOT) else overlay_path}")

    problems = validate_site(site)
    if problems:
        for problem in problems:
            bad(problem)
        return
    ok("site file is valid")

    derived = site_to_env(site)

    # Start from the shipped defaults so every variable exists, then apply the
    # site file on top, then generate the secrets.
    lines = ENV_EXAMPLE.read_text().splitlines()
    out, seen = [], set()
    for line in lines:
        match = re.match(r"^([A-Z0-9_]+)=", line)
        if match and match.group(1) in derived:
            key = match.group(1)
            out.append(f"{key}={derived[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = set(derived) - seen
    if missing:
        # A derived key with nowhere to land would be silently dropped.
        bad(f"these derived settings have no variable in .env.example: {sorted(missing)}")
        return

    text = "\n".join(out) + "\n"
    for var in GENERATED:
        text = re.sub(rf"^{var}=CHANGEME\s*$", f"{var}={secrets.token_hex(24)}", text, flags=re.M)

    existing = read_env()
    if ENV.exists():
        # Keep the secrets already in use — regenerating them would orphan the
        # data in Neo4j and Postgres.
        kept = 0
        for var in GENERATED:
            if existing.get(var) and existing[var] != "CHANGEME":
                text = re.sub(rf"^{var}=.*$", f"{var}={existing[var]}", text, flags=re.M)
                kept += 1
        if kept:
            info(f"kept {kept} existing secrets — regenerating them would orphan stored data")

    ENV.write_text(text)
    os.chmod(ENV, 0o600)

    count = dig(site, "scale.expected_devices", 50)
    ok(f".env generated from {site_path.name}")
    info(f"{derived['DEPLOYMENT_NAME']} · {count} devices · polling every "
         f"{derived['SNMP_INTERVAL']} · shards of {derived['SNMP_SHARD_SIZE']}")
    info(f"devices send to {derived['SYSLOG_COLLECTOR_IP']}: "
         f"syslog {derived['SYSLOG_PORT']}, netflow {derived['NETFLOW_PORT']}, "
         f"ipfix {derived['IPFIX_PORT']}")

    report_ai_platform(derived, overlay_path.name if overlay_path else site_path.name)

    leftover = [v for v in REQUIRED if re.search(rf"^{v}=CHANGEME\s*$", text, re.M)]
    if leftover:
        bad(f"still unset after the site file: {', '.join(leftover)}")


def configure_interactive(args) -> None:
    if not ENV.exists():
        shutil.copy(ENV_EXAMPLE, ENV)
        ok("created .env from .env.example")
    else:
        ok(".env already exists — leaving your values alone")

    text = ENV.read_text()

    # Generate anything still CHANGEME that we can generate ourselves.
    made = []
    for var in GENERATED:
        if re.search(rf"^{var}=CHANGEME\s*$", text, re.M):
            text = re.sub(rf"^{var}=CHANGEME\s*$", f"{var}={secrets.token_hex(24)}", text, flags=re.M)
            made.append(var)
    if made:
        ENV.write_text(text)
        ok(f"generated {len(made)} secrets: {', '.join(made)}")

    # Anything left needs a human.
    text = ENV.read_text()
    outstanding = [v for v in REQUIRED if re.search(rf"^{v}=CHANGEME\s*$", text, re.M)]
    if not outstanding:
        ok("all required values are set")
        return

    if args.yes:
        for var in outstanding:
            bad(f"{var} is unset — {REQUIRED[var]}")
        print(f"\n  {C['y']}--yes was given, so nothing was prompted. Edit .env and re-run.{C['x']}")
        return

    print(f"\n  {len(outstanding)} value(s) need you. Press Enter to skip any and edit .env later.\n")
    suggestion = detect_ip()
    for var in outstanding:
        hint = f" [{suggestion}]" if var == "SYSLOG_COLLECTOR_IP" and suggestion else ""
        try:
            answer = input(f"  {C['b']}{var}{C['x']} — {REQUIRED[var]}{hint}\n  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not answer and var == "SYSLOG_COLLECTOR_IP" and suggestion:
            answer = suggestion
        if answer:
            text = re.sub(rf"^{var}=CHANGEME\s*$", f"{var}={answer}", text, flags=re.M)
            ENV.write_text(text)

    still = [v for v in REQUIRED if re.search(rf"^{v}=CHANGEME\s*$", ENV.read_text(), re.M)]
    if still:
        warn(f"still unset: {', '.join(still)} — the stack will start but those "
             f"feeds will not work until you edit .env")
    else:
        ok("all required values are set")


def detect_ip() -> str | None:
    """The address a device would reach this host on.

    Opens a UDP socket to a public address and reads back which local interface
    the kernel chose. Nothing is sent — UDP connect() only sets the route.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        return addr if not addr.startswith("127.") else None
    except Exception:
        return None


# ------------------------------------------------------------ 3. build / 4. start

def build(args) -> None:
    head(3, "Build images")
    info("first run pulls ~2 GB and builds two images — several minutes")
    if stream(["docker", "compose", "build"]) != 0:
        bad("build failed — see the output above")
    else:
        ok("images built")


def start(args) -> None:
    head(4, "Start the stack")
    info("waiting until every container reports healthy (Neo4j is the slow one)")
    rc = stream(["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "600"])
    if rc != 0:
        bad("some containers did not become healthy")
        run(["docker", "compose", "ps"], check=False, capture=False)
        return
    try:
        out = run(["docker", "compose", "ps", "--services"]).stdout.split()
        ok(f"{len(out)} services running and healthy")
    except Exception:
        ok("stack started")


# ---------------------------------------------------------------- 5. initialise

def initialise(args) -> None:
    head(5, "Load schema and inventory")
    env = read_env()
    token = env.get("INFRAHUB_ADMIN_TOKEN", "")

    steps = [
        ("schema", ["docker", "compose", "exec", "-T",
                    "-e", "INFRAHUB_ADDRESS=http://infrahub-server:8000",
                    "-e", f"INFRAHUB_API_TOKEN={token}",
                    "infrahub-server", "infrahubctl", "schema", "load", "/schema/darqcube.yml"]),
        ("seed", ["docker", "compose", "exec", "-T", "infrahub-server", "python", "/scripts/seed.py"]),
        ("render", ["docker", "compose", "exec", "-T", "infrahub-server",
                    "python", "/scripts/render-inventory.py"]),
    ]
    for name, cmd in steps:
        try:
            result = run(cmd, check=False, timeout=300)
            if result.returncode == 0:
                last = [l for l in result.stdout.strip().splitlines() if l.strip()]
                ok(f"{name}: {last[-1][:90] if last else 'done'}")
            elif name == "render" and "no active devices" in result.stderr:
                # A fresh install has no inventory — the repo ships only
                # examples, which are never seeded. Nothing to render is the
                # expected state until the operator adds their devices.
                ok("render: skipped — no devices yet (see Next, below)")
            else:
                detail = (result.stderr or result.stdout).strip().splitlines()
                bad(f"{name} failed: {detail[-1][:120] if detail else '?'}")
        except subprocess.TimeoutExpired:
            bad(f"{name} timed out")


# ---------------------------------------------------------------- 6. verify

def verify(args) -> None:
    head(6, "Verify")

    python = VENV / "bin" / "python"
    if not python.exists():
        info("creating .venv for the test suite")
        rc = subprocess.run([sys.executable, "-m", "venv", str(VENV)], cwd=ROOT).returncode
        if rc != 0:
            bad("could not create .venv — install python3-venv (apt install python3-venv)")
            return
    deps = ["pytest", "pyyaml", "requests", "textfsm", "ntc-templates", "ttp", "deepdiff"]
    subprocess.run([str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                   cwd=ROOT, capture_output=True)
    rc = subprocess.run([str(python), "-m", "pip", "install", "-q", *deps],
                        cwd=ROOT, capture_output=True)
    if rc.returncode != 0:
        bad(f"could not install test dependencies: {rc.stderr.decode()[-200:]}")
        return
    ok("test environment ready")

    print()
    rc = subprocess.run([str(python), "-m", "pytest", "automation/tests", "-q",
                         "--tb=short", "-m", "not devices"], cwd=ROOT).returncode
    print()
    if rc == 0:
        ok("every check passed")
    else:
        bad("some checks failed — see above, and docs/how-to/troubleshooting.md")


# ---------------------------------------------------------------- 7. report

def report(args) -> None:
    head(7, "Ready")
    env = read_env()
    host = env.get("SYSLOG_COLLECTOR_IP") or detect_ip() or "localhost"

    print(f"""
  {C['b']}Open{C['x']}
    Grafana        http://{host}:{env.get('GRAFANA_PORT', '3000')}   (admin / see GRAFANA_ADMIN_PASSWORD in .env)
    Infrahub       http://{host}:{env.get('INFRAHUB_PORT', '8000')}
    Prometheus     http://{host}:{env.get('PROMETHEUS_PORT', '9090')}
    Alertmanager   http://{host}:{env.get('ALERTMANAGER_PORT', '9093')}
    Automation API http://{host}:{env.get('AUTOMATION_PORT', '8100')}/docs

  {C['b']}Point your devices at{C['x']}
    syslog         {host}:{env.get('SYSLOG_PORT', '514')}/udp or /tcp  (tcp: no silent loss)
    NetFlow        {host}:{env.get('NETFLOW_PORT', '2055')}/udp
    IPFIX          {host}:{env.get('IPFIX_PORT', '4739')}/udp

  {C['b']}Next{C['x']}
    1. cp source-of-truth/devices/examples/*.yml source-of-truth/devices/
       and replace the examples with your sites and devices
    2. make seed && make render
    3. Configure the devices themselves — docs/devices/
    4. make test-devices

  {C['d']}Everything you can change: docs/how-to/{C['x']}
""")


# ---------------------------------------------------------------- main

STEPS = [
    ("preflight", preflight), ("configure", configure), ("build", build),
    ("start", start), ("initialise", initialise), ("verify", verify), ("report", report),
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install DARQCUBE-AGENTICOPS-STACK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Common options:")[-1],
    )
    ap.add_argument("--from", dest="from_site", metavar="FILE",
                    help=f"generate .env from a site file (default: ./{SITE.name} if present)")
    ap.add_argument("--check", action="store_true", help="preflight only, change nothing")
    ap.add_argument("--yes", action="store_true", help="never prompt")
    ap.add_argument("--fix-sysctl", action="store_true", help="apply kernel settings and stop (needs sudo)")
    ap.add_argument("--skip-tests", action="store_true", help="stop after initialise")
    ap.add_argument("--step", type=int, metavar="N", help="run step N only (1-7)")
    ap.add_argument("--ai-platform", dest="ai_platform", metavar="FILE",
                    help=f"apply an ai-platform connection file (default: "
                         f"{AI_PLATFORM_OVERLAY.relative_to(ROOT)} if present)")
    args = ap.parse_args()

    print(f"{C['b']}DARQCUBE-AGENTICOPS-STACK{C['x']}")
    print(f"{C['d']}single-VM network telemetry, source of truth and automation{C['x']}")

    if not ENV_EXAMPLE.exists():
        print(f"\n{C['r']}Run this from the repository root — .env.example not found.{C['x']}")
        return 2

    if args.step:
        if not 1 <= args.step <= len(STEPS):
            print(f"--step must be 1..{len(STEPS)}")
            return 2
        STEPS[args.step - 1][1](args)
        return 1 if _failed else 0

    for n, (name, fn) in enumerate(STEPS, 1):
        # --fix-sysctl is a host-preparation mode, run once per VM and under sudo.
        # Continuing past preflight would do the whole install as root and leave
        # .env and the volumes root-owned.
        if (args.check or args.fix_sysctl) and n > 1:
            break
        if args.skip_tests and name in ("verify",):
            continue
        fn(args)
        # Stop before doing anything destructive on a host that is not ready.
        if _failed and name in ("preflight", "configure", "build"):
            print(f"\n{C['r']}Stopped at step {n} ({name}).{C['x']} "
                  f"Fix the failures above and re-run — completed steps are skipped automatically.")
            return 1

    if _failed:
        print(f"\n{C['y']}Finished with failures — see above.{C['x']}")
        return 1
    if args.fix_sysctl:
        print(f"\n{C['g']}Kernel settings applied.{C['x']} Now install as your normal user "
              f"(not root): python3 install.py")
    elif args.check:
        print(f"\n{C['g']}Preflight passed.{C['x']} Run without --check to install.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
