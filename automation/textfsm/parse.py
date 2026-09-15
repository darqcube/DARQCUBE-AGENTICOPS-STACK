"""The one entry point for turning raw CLI output into structured data.

Nothing parses inline. Every caller — the API, the Netmiko scripts, pytest and
(through the API) the MCP servers — goes through parse_output() so there is one
place where template resolution, failure handling and vendor differences live.

TEMPLATE RESOLUTION ORDER
  1. automation/textfsm/templates/ via the local index    (our templates win)
  2. the packaged ntc-templates library                   (~1000 templates)

Netmiko finds our directory through the NET_TEXTFSM environment variable, set
in compose/automation.yaml. See docs/how-to/add-a-textfsm-template.md for how to add one.
"""
from __future__ import annotations

import os
from pathlib import Path

import textfsm
import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates"
INDEX = HERE / "index"
PLATFORMS = Path(os.environ.get("PLATFORMS_FILE", HERE.parents[1] / "platforms.yml"))


class ParseError(RuntimeError):
    """Raised when output could not be parsed into rows.

    This is deliberately an ERROR and not an empty list. TextFSM returns [] for
    a template that does not match, and [] is indistinguishable from "the
    device has nothing to report" — so a missing or broken template would
    silently look like a healthy device with no interfaces. Failing loudly is
    the only way that surfaces.
    """


def _textfsm_platform(platform: str) -> str:
    """Infrahub platform -> the name ntc-templates keys its templates by.

    NOT the same as netmiko_type. Netmiko connects to IOS-XE as "cisco_xe",
    but ntc-templates files those templates under "cisco_ios" and has none
    named cisco_xe — so using the connection type here fails every Cisco parse.
    """
    with open(PLATFORMS) as fh:
        platforms = yaml.safe_load(fh)
    if platform not in platforms:
        raise ParseError(
            f"platform '{platform}' is not in platforms.yml "
            f"(have: {', '.join(sorted(platforms))})"
        )
    spec = platforms[platform]
    return spec.get("textfsm_platform") or spec["netmiko_type"]


def _local_template(device_type: str, command: str) -> Path | None:
    """Find a template in our own index.

    Index format (same as ntc-templates):
        Template, Hostname, Platform, Command

    The Command column may use the [...] abbreviation syntax, so
    "display int[erface] br[ief]" matches both the full and short forms.
    """
    if not INDEX.exists():
        return None

    import re

    for raw in INDEX.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("template"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        template, _hostname, platform, cmd_pattern = parts[0], parts[1], parts[2], ",".join(parts[3:])
        if platform != device_type:
            continue
        # "display int[erface] br[ief]" -> "display int(?:erface)? br(?:ief)?"
        regex = re.escape(cmd_pattern).replace(r"\[", "(?:").replace(r"\]", ")?")
        regex = regex.replace(r"\ ", r"\s+")
        if re.fullmatch(regex, command.strip()):
            path = TEMPLATE_DIR / template
            if path.exists():
                return path
    return None


def parse_output(platform: str, command: str, raw: str) -> list[dict]:
    """Parse CLI output into a list of dicts. Raises ParseError on failure.

    `platform` is the Infrahub platform value (ios_xe, vrp, routeros), not a
    netmiko device_type — callers should never need to know the mapping.
    """
    device_type = _textfsm_platform(platform)

    if not raw or not raw.strip():
        raise ParseError(f"{platform}: no output to parse for '{command}'")

    template = _local_template(device_type, command)
    if template:
        with open(template) as fh:
            fsm = textfsm.TextFSM(fh)
        rows = fsm.ParseText(raw)
        parsed = [dict(zip(fsm.header, row)) for row in rows]
        source = f"local:{template.name}"
    else:
        try:
            from ntc_templates.parse import parse_output as ntc_parse
        except ImportError as exc:  # pragma: no cover
            raise ParseError("ntc-templates is not installed") from exc
        try:
            parsed = ntc_parse(platform=device_type, command=command, data=raw)
        except Exception as exc:
            raise ParseError(
                f"no template for {device_type} '{command}' — neither in "
                f"automation/textfsm/templates/ nor in ntc-templates. "
                f"See docs/how-to/add-a-textfsm-template.md. ({exc})"
            ) from exc
        source = "ntc-templates"

    if not parsed:
        raise ParseError(
            f"{device_type} '{command}': template ({source}) matched no lines. "
            f"The device returned {len(raw.splitlines())} line(s), so this is a "
            f"template problem, not an empty device. First line: "
            f"{raw.splitlines()[0][:80]!r}"
        )
    return parsed


def available_templates() -> dict[str, list[str]]:
    """What the local index covers, per device_type. Used by tests and docs."""
    out: dict[str, list[str]] = {}
    if not INDEX.exists():
        return out
    for raw in INDEX.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("template"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            out.setdefault(parts[2], []).append(",".join(parts[3:]))
    return out
