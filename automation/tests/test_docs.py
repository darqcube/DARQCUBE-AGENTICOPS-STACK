"""Documentation consistency tests.

Install docs rot silently because nothing executes them. These check the claims
that are cheap to verify mechanically: that every `make` command the docs tell
you to run exists, that every internal link resolves, and that every .env
variable the docs reference is actually in .env.example.

Offline, no stack, no devices.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = sorted(ROOT.glob("docs/**/*.md")) + [ROOT / "README.md", ROOT / "CLAUDE.md"]


def makefile_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text()
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.M))


def env_example_vars() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.M))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_make_command_exists(doc):
    """A doc telling you to run `make foo` when there is no foo target is the
    most annoying kind of wrong."""
    targets = makefile_targets()
    # Only lines that are actually commands — "make a new group" is prose.
    used = set(re.findall(r"^\s*(?:\$ )?make ([a-z][a-z0-9-]*)", doc.read_text(), re.M))
    used |= set(re.findall(r"`make ([a-z][a-z0-9-]*)[`\s]", doc.read_text()))
    missing = used - targets
    assert not missing, f"{doc.name} references non-existent make targets: {missing}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_internal_links_resolve(doc):
    links = re.findall(r"\]\((?!https?://)([^)]+\.md)(?:#[^)]*)?\)", doc.read_text())
    broken = [l for l in links if not (doc.parent / l).exists()]
    assert not broken, f"{doc.name} has broken links: {broken}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_referenced_env_vars_exist(doc):
    """Catches a doc describing a setting that was renamed or never added."""
    known = env_example_vars()
    # Only ${VAR} / $VAR forms that look like our own settings.
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]{3,})(?::-[^}]*)?\}", doc.read_text()))
    # Variables set by the user's shell or by compose, not by .env.example.
    # Shell variables the READER'S environment supplies, not ours. EDITOR is
    # used as ${EDITOR:-nano} so the docs work whether or not it is set.
    external = {"PWD", "HOME", "USER", "EDITOR", "MAKEFILE_LIST", "COMPOSE_PROJECT_NAME"}
    # Placeholders in worked examples — a doc showing how to add a service
    # necessarily names variables that do not exist yet.
    placeholders = {"SOME_SETTING", "MY_SERVICE_PORT", "REQUIRED_SECRET", "VAR"}
    # TextFSM templates use ${VALUE} syntax that is not shell interpolation.
    textfsm_values = {"INTERFACE", "FLAGS", "TYPE", "NAME", "STATUS"}
    missing = used - known - external - placeholders - textfsm_values
    assert not missing, f"{doc.name} references unknown .env variables: {missing}"


@pytest.mark.parametrize("doc", sorted(ROOT.glob("docs/**/*.md")), ids=lambda p: p.name)
def test_referenced_repo_paths_exist(doc):
    """Docs name a lot of files. A stale path sends someone hunting."""
    text = doc.read_text()
    # Backticked paths that look like repo files, not commands or globs.
    candidates = re.findall(r"`((?:[a-z0-9_.-]+/)+[a-z0-9_.-]+\.(?:yml|yaml|py|conf|json|sh|tmpl|grok))`", text)
    missing = [c for c in set(candidates) if not (ROOT / c).exists()]
    # generated/ is created at runtime; .env and the inventory are per-install
    missing = [m for m in missing if "generated/" not in m and not m.endswith(".env")
               and not re.fullmatch(r"source-of-truth/devices/[^/]+\.ya?ml", m)]
    assert not missing, f"{doc.name} names files that do not exist: {sorted(missing)}"


def test_every_platform_has_an_onboarding_doc():
    """A platform without device-side instructions is unusable by anyone who
    did not build it."""
    platforms = yaml.safe_load((ROOT / "platforms.yml").read_text())
    for name, spec in platforms.items():
        doc = spec.get("onboarding_doc")
        assert doc, f"{name}: no onboarding_doc in platforms.yml"
        assert (ROOT / doc).exists(), f"{name}: onboarding_doc {doc} does not exist"


def test_docs_referenced_from_code_exist():
    """Error messages point people at documentation. A path that is wrong there
    is worse than a broken link in a doc — the reader is already stuck, and the
    one thing offered to help them 404s.

    """
    import re

    sources = []
    for pattern in ("automation/**/*.py", "mcp/**/*.py", "source-of-truth/**/*.py"):
        sources.extend(ROOT.glob(pattern))
    sources.append(ROOT / "platforms.yml")

    broken = []
    for src in sources:
        if ".venv" in str(src) or "__pycache__" in str(src):
            continue
        for ref in set(re.findall(r"docs/[A-Za-z0-9/_-]+\.md", src.read_text())):
            if not (ROOT / ref).exists():
                broken.append(f"{src.relative_to(ROOT)} -> {ref}")
    assert not broken, "docs referenced from code do not exist:\n  " + "\n  ".join(sorted(broken))


def test_every_how_to_is_in_the_index():
    """A guide nobody links to is a guide nobody finds."""
    index = (ROOT / "docs/how-to/README.md").read_text()
    for guide in sorted((ROOT / "docs/how-to").glob("*.md")):
        if guide.name == "README.md":
            continue
        assert guide.name in index, f"{guide.name} is not linked from docs/how-to/README.md"


def test_stated_counts_match_reality():
    """README quotes a container count and a guide count. Both drifted once —
    config-init was added and nobody updated the number, and the silent-loss
    guide made it 15. A number in prose has no way of noticing it is wrong."""
    readme = (ROOT / "README.md").read_text()

    guides = len([p for p in (ROOT / "docs/how-to").glob("*.md") if p.name != "README.md"])
    stated = re.search(r"(\d+) task guides", readme)
    assert stated, "README no longer states a guide count — remove this test or restore it"
    assert int(stated.group(1)) == guides, (
        f"README says {stated.group(1)} task guides, there are {guides}"
    )

    # Parsed from the compose files rather than a running stack, so this works
    # with nothing up. Parse the YAML — a regex over indented keys also picks
    # up `volumes:` entries and the x- anchor blocks.
    services = set()
    for path in (ROOT / "compose").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text()) or {}
        services |= set((doc.get("services") or {}).keys())
    stated = re.search(r"(\d+) containers in four groups", readme)
    assert stated, "README no longer states a container count"
    assert int(stated.group(1)) == len(services), (
        f"README says {stated.group(1)} containers, compose defines {len(services)}: "
        f"{sorted(services)}"
    )


def test_nothing_depends_on_mcp():
    """README says MCP sits at the bottom of the dependency graph with no
    inbound edges, so dropping the `mcp` profile loses no other feature. Two
    kinds of edge would break that: a service that waits for an MCP container,
    and code or config outside mcp/ that calls one."""
    services = {}
    for path in (ROOT / "compose").glob("*.yaml"):
        services |= (yaml.safe_load(path.read_text()) or {}).get("services") or {}
    mcp = {name for name in services if name.startswith("mcp-")}
    assert mcp, "no mcp-* services found — test is not looking in the right place"

    waits = [f"{name} -> {dep}" for name, spec in services.items() if name not in mcp
             for dep in (spec.get("depends_on") or []) if dep in mcp]
    assert not waits, f"services depend on MCP: {waits}"

    # An address (mcp-loki:9003), not a name in a comment.
    address = re.compile(r"\bmcp-[a-z]+:\d+")
    calls = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if (not path.is_file() or rel.parts[0] in {"mcp", ".git", ".venv", "docs"}
                or "tests" in rel.parts or "__pycache__" in rel.parts
                or path.suffix not in {".py", ".yml", ".yaml", ".conf", ".json", ".tmpl"}
                or rel == Path("compose/mcp.yaml")):
            continue
        if address.search(path.read_text(errors="ignore")):
            calls.append(str(rel))
    assert not calls, f"outside mcp/, these call an MCP server: {calls}"


def test_readme_and_claude_cover_the_current_toolchain():
    """Both are entry points. A tool that is in the stack but in neither file
    is a tool the next person will not know exists."""
    readme = (ROOT / "README.md").read_text()
    claude = (ROOT / "CLAUDE.md").read_text()
    reqs = (ROOT / "automation/service/requirements.txt").read_text()

    # Things that shape how someone works in this repo.
    for tool in ("Nornir", "Netmiko", "TextFSM", "TTP"):
        assert tool in readme, f"README does not mention {tool}"
        assert tool in claude, f"CLAUDE.md does not mention {tool}"

    # And anything load-bearing in requirements should be discoverable.
    for pkg, label in (("ttp", "TTP"), ("deepdiff", "DeepDiff")):
        assert pkg in reqs
        assert label in readme, f"{label} is a dependency but README never names it"


def test_every_component_has_an_install_page():
    """Every published service should be documented somewhere in docs/install/."""
    pages = " ".join(p.read_text() for p in (ROOT / "docs/install").glob("*.md"))
    for service in ("infrahub", "telegraf", "logstash", "prometheus", "loki",
                    "grafana", "alertmanager", "automation", "mcp"):
        assert service in pages.lower(), f"{service} is not covered in docs/install/"


def test_prerequisites_cover_every_external_command_used():
    """A tool the project shells out to but never tells you to install is a
    `command not found` in someone else's terminal. `jq` was exactly that:
    five make targets used it, and no document mentioned it."""
    import re as _re

    sources = [ROOT / "Makefile"]
    sources += list((ROOT / "scripts").glob("*.sh"))
    sources += list((ROOT / "source-of-truth/scripts").glob("*.sh"))

    # Commands the project invokes that are NOT in a base Ubuntu Server image.
    # openssl is deliberately NOT here: gen-secrets.sh uses python3, which is
    # already required, rather than adding a package for one random string.
    not_preinstalled = {"make", "jq", "git", "curl"}
    used = set()
    for src in sources:
        text = src.read_text()
        for tool in not_preinstalled:
            # A command at the start of a line, after a pipe, or after $( .
            if _re.search(rf"(^|\||\$\(|&&|\s){tool}\s", text, _re.M):
                used.add(tool)

    prereq = (ROOT / "docs/install/01-prerequisites.md").read_text()
    missing = [t for t in used if t not in prereq]
    assert not missing, (
        f"these are invoked by the project but absent from the prerequisites "
        f"page: {sorted(missing)}"
    )


def test_the_apt_line_matches_the_prerequisites_table():
    """The copy-paste line and the explanation must not drift apart."""
    import re as _re

    prereq = (ROOT / "docs/install/01-prerequisites.md").read_text()
    apt = _re.search(r"apt install -y ([a-z0-9 .-]+)", prereq)
    assert apt, "no apt install line in the prerequisites page"
    packages = set(apt.group(1).split())

    for doc in ("README.md", "docs/INSTALL.md"):
        text = (ROOT / doc).read_text()
        other = _re.search(r"apt install -y ([a-z0-9 .-]+)", text)
        assert other, f"{doc} has no apt install line"
        assert set(other.group(1).split()) == packages, (
            f"{doc} installs {set(other.group(1).split())}, "
            f"prerequisites page says {packages}"
        )


def test_architecture_volume_section_matches_compose():
    """docs/architecture.md lists every named volume and every bind mount by
    name. Prose like that drifts the moment a service gains a volume, so both
    directions are checked: nothing in compose is undocumented, and nothing
    documented has disappeared from compose."""
    arch = (ROOT / "docs/architecture.md").read_text()
    section = arch[arch.index("## Volumes and storage"):arch.index("## Two deliberate limits")]

    named, binds = set(), set()
    for path in (ROOT / "compose").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text()) or {}
        named |= set((doc.get("volumes") or {}).keys())
        for spec in (doc.get("services") or {}).values():
            for m in spec.get("volumes", []) or []:
                src = m.split(":")[0]
                if src.startswith("../"):
                    binds.add(src[3:].rstrip("/"))

    # Checked against TABLE ROWS, not the section as a whole: a volume can be
    # mentioned in prose ("back up loki-data") while missing from its table,
    # and the first version of this test passed in exactly that case.
    rows = {r.rstrip("/") for r in re.findall(r"^\|\s*`([^`]+)`\s*\|", section, re.M)}

    for v in named:
        assert v in rows, f"named volume {v} is in compose but has no table row"
    for b in binds:
        # automation/ is mounted whole; its row documents automation/configs/,
        # the only part the container writes.
        assert any(r == b or r.startswith(b + "/") for r in rows), (
            f"bind mount {b} is in compose but has no table row"
        )

    documented = {r for r in rows if re.fullmatch(r"[a-z0-9-]+-(?:data|logs|storage|config)", r)}
    stale = documented - named
    assert not stale, f"documented volumes no longer in compose: {stale}"


def test_prepare_script_installs_what_the_docs_list():
    """scripts/prepare-ubuntu.sh and the prerequisites page must name the same
    packages, or one route leaves a host without something the other has."""
    import re as _re

    script = (ROOT / "scripts/prepare-ubuntu.sh").read_text()
    listed = _re.search(r"^PACKAGES=\(([^)]*)\)", script, _re.M)
    assert listed, "no PACKAGES=(...) array in prepare-ubuntu.sh"
    prereq = (ROOT / "docs/install/01-prerequisites.md").read_text()
    apt = _re.search(r"apt install -y ([a-z0-9 .-]+)", prereq)
    assert set(listed.group(1).split()) == set(apt.group(1).split())
    assert "include:" in script and 'COMPOSE_MIN="2.20"' in script, \
        "the script must enforce the Compose version compose.yaml needs"


def test_prepare_script_is_executable():
    import os
    assert os.access(ROOT / "scripts/prepare-ubuntu.sh", os.X_OK)


def test_prepare_script_and_prerequisites_agree_on_the_ntp_wait():
    """Both routes must make Docker wait for the first NTP sync, or a VM that
    boots with a wrong clock poisons Prometheus with future-dated samples."""
    script = (ROOT / "scripts/prepare-ubuntu.sh").read_text()
    prereq = (ROOT / "docs/install/01-prerequisites.md").read_text()
    for text, where in ((script, "prepare-ubuntu.sh"), (prereq, "01-prerequisites.md")):
        assert "systemd-time-wait-sync" in text, where
        assert "After=time-sync.target" in text, where
        assert "docker.service.d/wait-for-time.conf" in text, where


def test_make_targets_that_call_the_api_fail_loudly():
    """`curl -sf ... | jq .` printed nothing on an API error and still exited 0,
    hiding a broken inventory behind a quiet `make state`."""
    import re as _re

    make = (ROOT / "Makefile").read_text()
    recipes = [l for l in make.splitlines() if "AUTOMATION_PORT)/device" in l]
    assert recipes
    assert not any("curl -sf" in l for l in recipes), "curl -sf hides the API's error"
    for line in make.splitlines():
        if line.startswith("\t@") and "|" in line and ("curl" in line or "jq -Rs" in line):
            assert "set -o pipefail" in line, f"piped recipe without pipefail: {line.strip()}"
