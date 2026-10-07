"""This repo is a public template — nothing from one deployment belongs in it.

CLAUDE.md says so; this makes it checkable. Two things leak most easily when
connecting the stack to a real ai-platform in a real lab:

- an address from that lab. Examples use documentation addresses (RFC 5737,
  192.0.2.0/24 etc.) or 10.0.0.0/8 — never 172.16/12 or 192.168/16, which is
  where lab and home networks actually live.
- the name of one particular ai-platform product. The stack works with any
  MCP client; code and docs say "ai-platform".

Offline. Scans tracked files plus new ones not yet ignored.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LAB_ADDRESS = re.compile(r"\b(?:172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")
PRODUCT = re.compile("ca" "ipe", re.I)          # split so this file does not match itself
# Genie's own golden fixtures: third-party test data, copied verbatim.
EXEMPT_DIRS = ("automation/pyats/samples/",)


def files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / f for f in out.stdout.splitlines()
            if not f.startswith(EXEMPT_DIRS) and (ROOT / f).is_file()]


def text_of(path: Path) -> str | None:
    try:
        return path.read_text()
    except (UnicodeDecodeError, OSError):
        return None


def test_no_lab_or_home_network_addresses():
    hits = []
    for path in files():
        text = text_of(path)
        if text:
            hits += [f"{path.relative_to(ROOT)}: {m}" for m in LAB_ADDRESS.findall(text)]
    assert not hits, "use 192.0.2.0/24 (RFC 5737) or 10.0.0.0/8 in examples:\n" + "\n".join(hits)


def test_the_ai_platform_is_not_named():
    hits = []
    for path in files():
        text = text_of(path)
        if text and PRODUCT.search(text):
            hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"say 'ai-platform', not a product name: {hits}"
