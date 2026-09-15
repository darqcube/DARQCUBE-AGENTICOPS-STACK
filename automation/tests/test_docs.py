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
    external = {"PWD", "HOME", "USER", "MAKEFILE_LIST", "COMPOSE_PROJECT_NAME"}
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
    # generated/ is created at runtime; .env is per-install
    missing = [m for m in missing if "generated/" not in m and not m.endswith(".env")]
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
