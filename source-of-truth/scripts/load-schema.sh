#!/usr/bin/env bash
# Load the Infrahub schema. Run via: make schema
#
# Uses infrahubctl inside the running infrahub-server container, which already
# has the CLI and the credentials — no extra tooling needed on the host.
set -euo pipefail

cd "$(dirname "$0")/../.."

if [[ ! -f .env ]]; then
  echo "!! no .env — cp .env.example .env and fill it in first" >&2
  exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

: "${INFRAHUB_ADMIN_TOKEN:?set INFRAHUB_ADMIN_TOKEN in .env}"

echo "Loading schema from source-of-truth/schema/ ..."

# Schema files are already mounted at /schema in the container (see
# compose/source-of-truth.yaml), so this needs no volume juggling.
docker compose exec -T \
  -e INFRAHUB_ADDRESS=http://infrahub-server:8000 \
  -e INFRAHUB_API_TOKEN="${INFRAHUB_ADMIN_TOKEN}" \
  infrahub-server \
  infrahubctl schema load /schema/darqcube.yml

echo
echo "Schema loaded. Next: make seed"
