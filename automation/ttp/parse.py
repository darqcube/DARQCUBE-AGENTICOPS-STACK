"""TTP parsing — structured data out of CONFIGURATION.

TextFSM and TTP do different jobs, and this stack uses both:

    TextFSM (automation/textfsm/)  tabular `show` output — one row per line,
                                   fixed columns. Interfaces, routes, ARP.
    TTP     (this module)          hierarchical CONFIG — indentation-scoped
                                   blocks, repeated sections, nested values.

Running-config is a tree, not a table. TextFSM has no notion of a block that
contains other blocks, so parsing "every interface stanza with its description
and its ACLs" is exactly what TTP is for.

Templates live in templates/ and are selected by Infrahub platform name.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates"
PLATFORMS = Path(os.environ.get("PLATFORMS_FILE", HERE.parents[1] / "platforms.yml"))


class TTPParseError(RuntimeError):
    """Raised when a config could not be parsed into structure.

    Like the TextFSM layer, an empty result is an ERROR rather than a silent
    empty list — a template that stops matching otherwise looks exactly like a
    device with no configuration.
    """


def template_for(platform: str, kind: str = "interfaces") -> Path:
    """templates/<platform>-<kind>.txt"""
    path = TEMPLATE_DIR / f"{platform}-{kind}.txt"
    if not path.exists():
        available = sorted(p.name for p in TEMPLATE_DIR.glob("*.txt"))
        raise TTPParseError(
            f"no TTP template for {platform} '{kind}'. "
            f"Expected {path.name}. Available: {', '.join(available) or 'none'}. "
            f"See docs/how-to/add-a-ttp-template.md"
        )
    return path


def parse_config(platform: str, raw: str, kind: str = "interfaces") -> list[dict]:
    """Parse a device configuration into structured rows."""
    from ttp import ttp

    if not raw or not raw.strip():
        raise TTPParseError(f"{platform}: no configuration to parse")

    with open(PLATFORMS) as fh:
        platforms = yaml.safe_load(fh)
    if platform not in platforms:
        raise TTPParseError(f"platform '{platform}' is not in platforms.yml")

    parser = ttp(data=raw, template=str(template_for(platform, kind)))
    parser.parse()
    result = parser.result()

    # TTP nests results per input and per template. Flatten to a list of dicts.
    rows: list[dict] = []
    for per_input in result:
        for item in per_input if isinstance(per_input, list) else [per_input]:
            if isinstance(item, list):
                rows.extend(r for r in item if isinstance(r, dict))
            elif isinstance(item, dict):
                rows.append(item)

    if not rows:
        raise TTPParseError(
            f"{platform} '{kind}': template matched nothing. The config has "
            f"{len(raw.splitlines())} line(s), so this is a template problem, "
            f"not an empty device."
        )
    return rows


def available_templates() -> dict[str, list[str]]:
    """{platform: [kind, ...]} — what we can currently parse."""
    out: dict[str, list[str]] = {}
    for path in sorted(TEMPLATE_DIR.glob("*.txt")):
        platform, _, kind = path.stem.partition("-")
        out.setdefault(platform, []).append(kind)
    return out
