"""scripts/ai-platform-connect.py — generated on the ai-platform host, read by
install.py on the stack VM. Offline: a throwaway local HTTP server plays the
identity provider.

The contract that matters: what the generator writes, install.py accepts —
same reader, same validation — and the issuer survives untouched while only
the key URL is rewritten to something the VM can reach.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ai-platform-connect.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return load(SCRIPT, "ai_platform_connect")


@pytest.fixture(scope="module")
def inst():
    return load(ROOT / "install.py", "installer")


@pytest.fixture
def idp():
    """An OpenID discovery endpoint on loopback, issuer naming localhost."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            body = json.dumps({
                "issuer": f"http://localhost:{port}/realms/demo",
                "jwks_uri": f"http://localhost:{port}/realms/demo/protocol/openid-connect/certs",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/realms/demo"
    server.shutdown()


def test_uses_only_the_standard_library():
    """It runs on the ai-platform host from a bare clone, before any pip."""
    tree = ast.parse(SCRIPT.read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    third_party = imported - set(sys.stdlib_module_names) - {"__future__"}
    assert not third_party, f"non-stdlib imports: {third_party}"


@pytest.mark.parametrize("uri,expected", [
    ("http://localhost:7080/realms/r/certs", "http://192.0.2.1:7080/realms/r/certs"),
    ("http://127.0.0.1:7080/realms/r/certs", "http://192.0.2.1:7080/realms/r/certs"),
    ("http://idp:8080/realms/r/certs", "http://192.0.2.1:8080/realms/r/certs"),
    ("https://idp.example.com/realms/r/certs", "https://idp.example.com/realms/r/certs"),
])
def test_only_unreachable_key_hosts_are_rewritten(gen, uri, expected):
    assert gen.reachable_jwks(uri, "192.0.2.1") == expected


def test_end_to_end_output_is_what_install_accepts(gen, inst, idp, tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--stack-host", "127.0.0.1", "--idp", idp,
         "--jwks-host", "192.0.2.1", "--audience", "ai-platform", "--token-header", "X-Test-Token",
         "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr

    overlay_path = tmp_path / "ai-platform.site.yml"
    overlay = inst.read_yaml(overlay_path)
    assert overlay == yaml.safe_load(overlay_path.read_text()), "install.py reads it differently from YAML"
    assert not inst.validate_ai_platform(overlay), inst.validate_ai_platform(overlay)

    auth = overlay["ai_platform"]["auth"]
    assert auth["mode"] == "oidc"
    assert auth["issuer"].startswith("http://localhost:"), "the issuer must stay as tokens carry it"
    assert auth["jwks_url"].startswith("http://192.0.2.1:"), "keys must be fetched from a reachable host"
    assert overlay["ai_platform"]["allow_write"] is False
    assert oct(overlay_path.stat().st_mode & 0o777) == "0o600"

    env = inst.ai_platform_env(inst.merge_overlay({}, overlay, "o"), "192.0.2.10", "13000")
    assert env["MCP_BIND_IP"] == "192.0.2.10" and env["MCP_AUTH_MODE"] == "oidc"


def test_snippet_registers_all_seven_servers(gen, tmp_path):
    snippet = yaml.safe_load(gen.mcp_snippet("192.0.2.10", "X-Test-Token"))
    servers = snippet["mcp_servers"]
    assert [s["endpoint"] for s in servers] == [f"http://192.0.2.10:{p}/mcp" for p in range(9001, 9008)]
    assert all(s["transport"] == "http" for s in servers)
    assert all(s["credential_sources"][0]["name"] == "X-Test-Token" for s in servers)


def test_snippet_matches_the_servers_compose_defines(gen):
    compose = yaml.safe_load((ROOT / "compose" / "mcp.yaml").read_text())["services"]
    ports = {name.removeprefix("mcp-"): int(spec["environment"]["PORT"]) for name, spec in compose.items()}
    assert {name: port for name, port, _ in gen.SERVERS} == ports


def test_an_unreachable_idp_fails_clearly(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--stack-host", "127.0.0.1",
         "--idp", "http://127.0.0.1:9/realms/none", "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 1 and "cannot read" in out.stderr
    assert not (tmp_path / "ai-platform.site.yml").exists()
