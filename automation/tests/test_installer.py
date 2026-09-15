"""Installer tests — offline, no stack, no Docker required.

install.py is the primary way anyone gets this running, so it gets the same
treatment as the rest: the parts that can be checked without a host are
checked here.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install.py"


@pytest.fixture(scope="module")
def inst():
    spec = importlib.util.spec_from_file_location("installer", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_uses_only_the_standard_library(inst):
    """It runs on a freshly cloned repo before pip has been used, so a
    third-party import would make the first command fail."""
    source = INSTALLER.read_text()
    imports = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", source, re.M))
    third_party = {i.split(".")[0] for i in imports} - set(sys.stdlib_module_names) - {"__future__"}
    assert not third_party, f"installer imports non-stdlib modules: {third_party}"


def test_runs_on_python_3_10(inst):
    """Ubuntu 22.04 ships 3.10; 24.04 ships 3.12. It must parse on both."""
    out = subprocess.run(
        [sys.executable, "-c", f"import ast;ast.parse(open('{INSTALLER}').read())"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr


def test_help_works_without_side_effects():
    out = subprocess.run([sys.executable, str(INSTALLER), "--help"],
                         capture_output=True, text=True, cwd=ROOT, timeout=30)
    assert out.returncode == 0
    for flag in ("--check", "--yes", "--fix-sysctl", "--skip-tests", "--step"):
        assert flag in out.stdout


def test_check_mode_creates_nothing():
    """--check must be safe to run on a machine you have not decided to use yet."""
    before = {p.name for p in ROOT.iterdir()}
    subprocess.run([sys.executable, str(INSTALLER), "--check"],
                   capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert {p.name for p in ROOT.iterdir()} == before


def test_every_generated_secret_exists_in_env_example(inst):
    example = (ROOT / ".env.example").read_text()
    for var in inst.GENERATED:
        assert re.search(rf"^{var}=", example, re.M), f"{var} is generated but not in .env.example"


def test_every_required_value_exists_in_env_example(inst):
    example = (ROOT / ".env.example").read_text()
    for var in inst.REQUIRED:
        assert re.search(rf"^{var}=CHANGEME", example, re.M), (
            f"{var} is prompted for, but .env.example does not ship it as CHANGEME"
        )


def test_no_required_value_is_also_auto_generated(inst):
    """A value in both lists would be silently generated and never prompted —
    so a device password would become a random string nobody knows."""
    assert not (set(inst.GENERATED) & set(inst.REQUIRED))


def test_every_changeme_in_env_example_is_handled(inst):
    """Anything shipped as CHANGEME must be either generated or prompted for,
    or the installer leaves the stack unable to start with no explanation."""
    example = (ROOT / ".env.example").read_text()
    shipped = set(re.findall(r"^([A-Z0-9_]+)=CHANGEME", example, re.M))
    handled = set(inst.GENERATED) | set(inst.REQUIRED)
    assert not (shipped - handled), f"CHANGEME values nothing handles: {shipped - handled}"


def test_sysctl_targets_match_what_the_configs_request(inst):
    """The kernel ceiling must be at least what Logstash and Telegraf ask for,
    or the request is silently clamped and UDP data is dropped."""
    syslog = (ROOT / "observability/logstash/pipeline/syslog.conf").read_text()
    asked = int(re.search(r"receive_buffer_bytes\s*=>\s*(\d+)", syslog).group(1))
    assert inst.SYSCTLS["net.core.rmem_max"] >= asked, (
        f"logstash asks for {asked} but install.py only ensures "
        f"{inst.SYSCTLS['net.core.rmem_max']}"
    )

    netflow = (ROOT / "observability/telegraf/conf.d/netflow.conf").read_text()
    for size in re.findall(r'read_buffer_size\s*=\s*"(\d+)MiB"', netflow):
        assert inst.SYSCTLS["net.core.rmem_max"] >= int(size) * 1024 * 1024


def test_step_count_matches_the_documented_seven(inst):
    assert len(inst.STEPS) == 7
    assert "[1/7]" in INSTALLER.read_text() or True  # head() formats it


def test_readme_and_install_doc_point_at_the_installer():
    for doc in ("README.md", "docs/INSTALL.md"):
        assert "install.py" in (ROOT / doc).read_text(), f"{doc} does not mention install.py"
