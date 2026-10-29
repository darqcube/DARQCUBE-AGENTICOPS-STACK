#!/usr/bin/env bash
# `make writes-on` / `make writes-off`: whether an AI may change configuration on the devices.
# The stack owner's choice, made on this host: the ai-platform cannot set it (install.py ignores an overlay's
# allow_write). On, WRITE_MODE=approved (default): the change tools are registered — stage, then a person approves,
# then commit (docs/how-to/approve-changes.md). On, WRITE_MODE=direct: push_device_config, gated by the write role.
# Off: no change tool exists (tools/list does not offer one).
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in on) want=true ;; off) want=false ;; *) echo "usage: $0 on|off" >&2; exit 2 ;; esac
[ -f .env ] || { echo ".env not found: install first" >&2; exit 1; }

if grep -q '^MCP_ALLOW_WRITE=' .env; then
  tmp=$(mktemp .env.XXXXXX)   # same folder, so the move keeps it on one filesystem; never left behind
  trap 'rm -f "$tmp"' EXIT
  sed "s/^MCP_ALLOW_WRITE=.*/MCP_ALLOW_WRITE=$want/" .env > "$tmp" && cat "$tmp" > .env
else
  echo "MCP_ALLOW_WRITE=$want" >> .env
fi
echo "MCP_ALLOW_WRITE=$want in .env"

# With a site file, install.py rebuilds .env from it: it must agree, or the next install run reverts this.
if [ -f site.yml ] && ! grep -qE "^\s+allow_write:\s*$want\b" site.yml; then
  echo "note: site.yml does not say ai_platform.allow_write: $want — set it there too, or install.py reverts this"
fi

docker compose up -d --wait --force-recreate mcp-netmiko >/dev/null
python3 scripts/mcp-check.py | grep -E 'mcp-netmiko' || true
echo "netmiko: 2 tools = read only, 7 = approved changes (stage, commit…), 3 = push_device_config (WRITE_MODE=direct)"
