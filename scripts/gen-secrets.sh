#!/usr/bin/env bash
# Create .env from .env.example with generated secrets.
#
# Fills in everything that just needs to be long and random. Leaves the values
# only you can know — device credentials, SNMPv3 passphrases, the collector IP —
# as CHANGEME, so preflight will tell you what is still outstanding.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo "!! .env already exists. Refusing to overwrite it — your secrets are in there." >&2
  echo "   Delete it first if you really want to regenerate." >&2
  exit 1
fi

cp .env.example .env

# These are not a security feature — there is no TLS in this stack. Neo4j,
# RabbitMQ and Postgres simply refuse to start without a password, so something
# has to put one there. Generated with python3, which is already required;
# openssl would be one more package to install for no gain.
#
# Each gets its OWN value: reusing one string across services means a leak
# anywhere is a leak everywhere.
for var in NEO4J_PASSWORD RABBITMQ_PASSWORD POSTGRES_PASSWORD \
           INFRAHUB_SECRET_KEY INFRAHUB_ADMIN_TOKEN MCP_AUTH_TOKEN \
           GRAFANA_ADMIN_PASSWORD; do
  secret=$(python3 -c "import secrets; print(secrets.token_hex(24))")
  # The delimiter is | because a hex secret can never contain one.
  sed -i.bak "s|^${var}=.*|${var}=${secret}|" .env
done
rm -f .env.bak

echo "Generated .env with random secrets for:"
echo "  NEO4J_PASSWORD  RABBITMQ_PASSWORD  POSTGRES_PASSWORD"
echo "  INFRAHUB_SECRET_KEY  INFRAHUB_ADMIN_TOKEN  MCP_AUTH_TOKEN"
echo "  GRAFANA_ADMIN_PASSWORD"
echo
echo "Still to fill in yourself — these cannot be generated:"
# Only real assignments — the header comments mention CHANGEME too.
# [A-Z0-9_] not [A-Z_]: SNMPV3_AUTH has a digit, and omitting it silently
# dropped the SNMP credentials from this list.
grep -nE '^[A-Z0-9_]+=CHANGEME' .env | sed 's/^/  /' || echo "  (none)"
echo
echo "SYSLOG_COLLECTOR_IP must be this VM's routable IP — devices send to it."
echo "Find it with:  ip -4 addr show scope global"
echo
echo "Then run: ./scripts/preflight.sh"
