#!/usr/bin/env bash
# Verify a running stack. This is what `make verify` runs, and it is the same
# set of checks docs/INSTALL.md tells you to perform — kept in one place so the
# documentation and the command cannot drift apart.
#
# Exits non-zero on the first hard failure.
set -uo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "!! no .env — see docs/INSTALL.md" >&2; exit 1; }
set -a; source .env; set +a

: "${GRAFANA_PORT:=3000}"; : "${INFRAHUB_PORT:=8000}"
: "${PROMETHEUS_PORT:=9090}"; : "${ALERTMANAGER_PORT:=9093}"
: "${LOKI_PORT:=3100}"; : "${AUTOMATION_PORT:=8100}"

fail=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }

echo "Containers"
unhealthy=$(docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null \
  | awk '$2 != "running" || ($3 != "" && $3 != "healthy") {print $1}')
if [[ -n "$unhealthy" ]]; then
  bad "not healthy: $(echo "$unhealthy" | tr '\n' ' ')"
else
  ok "$(docker compose ps --services 2>/dev/null | wc -l | tr -d ' ') service(s) running and healthy"
fi

echo "Services answering"
check_http() {  # name url [expect-substring]
  body=$(curl -sf --max-time 5 "$2" 2>/dev/null) || { bad "$1 not answering ($2)"; return; }
  if [[ $# -ge 3 && "$body" != *"$3"* ]]; then bad "$1 answered unexpectedly"; else ok "$1"; fi
}
check_http "Prometheus"   "http://localhost:${PROMETHEUS_PORT}/-/ready"
check_http "Loki"         "http://localhost:${LOKI_PORT}/ready"
check_http "Alertmanager" "http://localhost:${ALERTMANAGER_PORT}/-/ready"
check_http "Grafana"      "http://localhost:${GRAFANA_PORT}/api/health" '"database"'
check_http "Infrahub"     "http://localhost:${INFRAHUB_PORT}/api/schema/summary"
if [[ "${COMPOSE_PROFILES:-}" == *automation* ]]; then
  check_http "Automation" "http://localhost:${AUTOMATION_PORT}/healthz" '"ok"'
fi

# The layer that matters: everything above can be green while nothing is joined.
echo "Components wired to each other"
targets=$(curl -sf --max-time 5 "http://localhost:${PROMETHEUS_PORT}/api/v1/targets" 2>/dev/null)
if [[ -z "$targets" ]]; then
  bad "cannot read Prometheus targets"
else
  down=$(printf '%s' "$targets" | grep -o '"health":"[a-z]*"' | grep -cv '"health":"up"' || true)
  [[ "$down" -eq 0 ]] && ok "all Prometheus scrape targets up" || bad "$down scrape target(s) down"
fi

devices=$(curl -sf --max-time 10 "http://localhost:${INFRAHUB_PORT}/graphql" \
  -H "X-INFRAHUB-KEY: ${INFRAHUB_ADMIN_TOKEN:-}" \
  -d '{"query":"{NetworkDevice{count}}"}' 2>/dev/null | grep -o '"count":[0-9]*' | cut -d: -f2)
if [[ -z "${devices:-}" ]]; then
  bad "cannot query Infrahub — is the schema loaded? (make schema)"
elif [[ "$devices" -eq 0 ]]; then
  warn "Infrahub has no devices — run: make seed"
else
  ok "$devices device(s) in the source of truth"
fi

if [[ -f observability/telegraf/generated/devices.json ]]; then
  rendered=$(grep -c '"device"' observability/telegraf/generated/devices.json || echo 0)
  [[ "$rendered" -gt 0 ]] && ok "collectors have $rendered identity entries" \
    || bad "identity table is empty — run: make render"
else
  warn "nothing rendered yet — run: make render"
fi

# The device label only exists if a log was received, parsed with the right
# vendor pattern, AND matched against the source of truth. One check, whole path.
labels=$(curl -sf --max-time 5 "http://localhost:${LOKI_PORT}/loki/api/v1/labels" 2>/dev/null)
if [[ "$labels" == *'"device"'* ]]; then
  ok "Loki has the device label — the whole syslog path works"
else
  warn "Loki has no device label yet — no syslog received, or devices are not configured"
fi

echo
if [[ $fail -eq 0 ]]; then
  echo "Verified."
else
  echo "FAILED — see docs/how-to/troubleshooting.md" >&2
  exit 1
fi
