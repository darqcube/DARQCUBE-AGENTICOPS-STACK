#!/usr/bin/env python3
"""Generate the files that connect an ai-platform to this stack.

Run this ON THE AI-PLATFORM HOST, from a clone of this repository. Everything
the stack needs to trust the ai-platform lives on that side — its identity
provider's issuer and signing keys, and the address the stack can reach it on
— so it is read there rather than typed by hand on the stack's VM.

    python3 scripts/ai-platform-connect.py \\
        --stack-host 192.0.2.10 \\
        --idp http://localhost:7080/realms/<realm>

Writes two files (default: the current directory):

    ai-platform.site.yml       copy to the stack VM as sites/ai-platform.yml,
                               then run install.py there — it merges the file
    mcp-servers.snippet.yaml   the seven MCP servers, to register in the
                               ai-platform's own MCP server configuration

Standard library only, like install.py: it runs before anything is installed.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "templates" / "ai-platform-mcp-servers.yaml.tmpl"

# name, port, what the ai-platform gets from it — the same table as
# docs/how-to/connect-an-ai-platform.md.
SERVERS = [
    ("infrahub", 9001, "Intended state: devices, sites, applications and their dependencies"),
    ("prometheus", 9002, "Live metrics, alerts, interface state and traffic flows"),
    ("loki", 9003, "Device syslog: search, recent errors, pattern counts"),
    ("grafana", 9004, "Dashboard links to hand a person"),
    ("netmiko", 9005, "Running config and parsed operational state from the device"),
    ("assurance", 9006, "Assurance rule checks, snapshots and parsed config"),
    ("pyats", 9007, "Genie structured models: BGP neighbors, interfaces, LLDP, platform"),
]
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def discover(idp: str) -> dict:
    """The provider's OpenID configuration: issuer and jwks_uri."""
    url = idp.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            config = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        fail(f"cannot read {url}: {exc}\n"
             f"  Is the ai-platform's identity provider running, and is --idp the realm URL?")
    for key in ("issuer", "jwks_uri"):
        if not config.get(key):
            fail(f"{url} has no '{key}'")
    return config


def address_towards(host: str) -> str:
    """This machine's address on the route to `host`.

    UDP connect() only selects a route; nothing is sent. That address is how
    the stack will reach this machine's identity provider for its keys.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError as exc:
        fail(f"no route to {host}: {exc}")


def reachable_jwks(jwks_uri: str, local_ip: str) -> str:
    """The JWKS URL with a loopback or container-only hostname replaced.

    The issuer often names `localhost` (it is what a browser on this machine
    uses), which from the stack VM means the VM itself. The ISSUER must stay
    exactly as the tokens carry it; only the URL the keys are fetched from
    changes.
    """
    parts = urllib.parse.urlsplit(jwks_uri)
    host = parts.hostname or ""
    if host in LOOPBACK_NAMES or "." not in host:
        netloc = local_ip + (f":{parts.port}" if parts.port else "")
        return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return jwks_uri


def token_claims(path: Path) -> dict:
    """Claims from a sample token — unverified, only to suggest settings."""
    try:
        payload = path.read_text().strip().split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (OSError, IndexError, ValueError) as exc:
        fail(f"{path} is not a JWT: {exc}")


def site_overlay(args, issuer: str, jwks_url: str, audiences: list[str]) -> str:
    """The ai_platform block, in the block-style YAML install.py reads."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Generated {stamp} by scripts/ai-platform-connect.py on the ai-platform host.",
        "# Copy to the stack VM as sites/ai-platform.yml (gitignored) and run install.py.",
        "# install.py merges this over site.yml, or applies it to an existing .env.",
        "ai_platform:",
        f"  allow_write: {'true' if args.allow_write else 'false'}",
        "  publish:",
        "    enabled: true",
        f'    bind_ip: "{args.bind_ip or ""}"',
        "  auth:",
        f"    mode: {args.auth_mode}",
        f'    issuer: "{issuer}"',
        f'    jwks_url: "{jwks_url}"',
        f'    audiences: "{",".join(audiences)}"',
        f"    write_role: {args.write_role}",
        f"  max_response_kb: {args.max_response_kb}",
        f"  graphs: {'true' if args.graphs else 'false'}",
    ]
    return "\n".join(lines) + "\n"


def mcp_snippet(stack_host: str, token_header: str) -> str:
    template = TEMPLATE.read_text()
    entries = []
    block = template.split("# --- per server ---\n", 1)[1]
    head = template.split("# --- per server ---\n", 1)[0]
    for name, port, description in SERVERS:
        entries.append(block.format(name=name, port=port, description=description,
                                    stack_host=stack_host, token_header=token_header))
    return head + "".join(entries)


def probe(host: str) -> list[str]:
    """Which MCP ports answer from here — before publishing, none will."""
    open_ports = []
    for name, port, _ in SERVERS:
        try:
            with socket.create_connection((host, port), timeout=2):
                open_ports.append(f"{name}:{port}")
        except OSError:
            pass
    return open_ports


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-host", required=True,
                    help="the stack VM's address, as reachable from this machine")
    ap.add_argument("--idp", required=True,
                    help="the identity provider realm URL tokens are issued by, "
                         "e.g. http://localhost:7080/realms/<realm>")
    ap.add_argument("--auth-mode", default="oidc", choices=("oidc", "both", "token"),
                    help="how the stack checks callers (default: oidc)")
    ap.add_argument("--audience", action="append", default=[],
                    help="an accepted 'aud' value; repeat for several (default: from "
                         "--token-file, else not checked)")
    ap.add_argument("--token-file", type=Path,
                    help="a sample access token, to suggest audiences and show its roles")
    ap.add_argument("--jwks-host",
                    help="address the stack uses to reach this machine (default: detected)")
    ap.add_argument("--bind-ip", default="",
                    help="stack VM address to publish MCP on (default: the VM's collector_ip)")
    ap.add_argument("--write-role", default="darqcube-write")
    ap.add_argument("--allow-write", action="store_true",
                    help="register push_device_config (still gated by --write-role)")
    ap.add_argument("--graphs", action="store_true",
                    help="graphs in chat: start Grafana's renderer and register render_interface_graph")
    ap.add_argument("--max-response-kb", type=int, default=64,
                    help="cap on one tool result (default 64, for small local models)")
    ap.add_argument("--token-header", default="",
                    help="request header your ai-platform forwards the caller's token in, "
                         "for the MCP server snippet (see its credential-source docs)")
    ap.add_argument("--stack-user", default="<user>", help="SSH user on the stack VM, for the copy command")
    ap.add_argument("--stack-dir", default="~/DARQCUBE-AGENTICOPS-STACK",
                    help="the repository's path on the stack VM")
    ap.add_argument("--out", type=Path, default=Path("."), help="where to write the two files")
    args = ap.parse_args()

    config = discover(args.idp)
    issuer = config["issuer"]
    local_ip = args.jwks_host or address_towards(args.stack_host)
    jwks_url = reachable_jwks(config["jwks_uri"], local_ip)

    audiences = list(args.audience)
    if args.token_file:
        claims = token_claims(args.token_file)
        if claims.get("iss") != issuer:
            print(f"warning: the sample token's iss {claims.get('iss')!r} differs from the "
                  f"provider's issuer {issuer!r} — the stack would refuse such tokens")
        aud = claims.get("aud") or []
        aud = [aud] if isinstance(aud, str) else aud
        roles = sorted(set((claims.get("realm_access") or {}).get("roles") or []))
        print(f"sample token: aud={aud} roles={roles}")
        if not audiences:
            audiences = aud
        if args.write_role not in roles:
            print(f"  note: this caller lacks '{args.write_role}', so push_device_config "
                  f"would be refused for them")

    args.out.mkdir(parents=True, exist_ok=True)
    overlay = args.out / "ai-platform.site.yml"
    snippet = args.out / "mcp-servers.snippet.yaml"
    overlay.write_text(site_overlay(args, issuer, jwks_url, audiences))
    overlay.chmod(0o600)
    snippet.write_text(mcp_snippet(args.stack_host, args.token_header or "<token-header>"))

    print(f"issuer     {issuer}")
    print(f"jwks_url   {jwks_url}   (as the stack will fetch it)")
    print(f"audiences  {','.join(audiences) or '(not checked)'}")
    print(f"wrote      {overlay}")
    print(f"wrote      {snippet}")
    already = probe(args.stack_host)
    print(f"MCP ports open on {args.stack_host} now: {', '.join(already) or 'none (expected before step 2)'}")
    print()
    print("Next:")
    print(f"  1. scp {overlay} {args.stack_user}@{args.stack_host}:{args.stack_dir}/sites/ai-platform.yml")
    print(f"     (create the directory first if needed: ssh {args.stack_user}@{args.stack_host} "
          f"mkdir -p {args.stack_dir}/sites)")
    then_up = " && make up" if args.graphs else ""
    print(f"  2. on the stack VM:  cd {args.stack_dir} && git pull && python3 install.py --step 2 && make mcp-apply{then_up}")
    print(f"  3. register the servers in {snippet} with the ai-platform")
    if not args.token_header:
        print("     — set --token-header to fill in the header the ai-platform forwards tokens in")
    print(f"  4. from the stack VM, check it can fetch the keys:  curl -s {jwks_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
