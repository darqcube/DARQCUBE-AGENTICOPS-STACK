#!/usr/bin/env bash
# Check the host and .env before starting. Run via: make preflight
#
# Everything here is a thing that, if wrong, produces a confusing failure
# later rather than an obvious one now.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; }

echo "Docker"
if ! command -v docker >/dev/null; then
  bad "docker not found — see docs/install/01-prerequisites.md"
else
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '(daemon not running)')"
  # `include:` in compose.yaml needs Compose v2.20 or newer.
  cv=$(docker compose version --short 2>/dev/null || echo 0)
  major=${cv%%.*}; rest=${cv#*.}; minor=${rest%%.*}
  if [[ ${major:-0} -gt 2 ]] || { [[ ${major:-0} -eq 2 ]] && [[ ${minor:-0} -ge 20 ]]; }; then
    ok "compose $cv (include: supported)"
  else
    bad "compose $cv is too old — compose.yaml uses 'include:', which needs v2.20+"
  fi
fi

echo "Resources"
if [[ "$(uname)" == "Darwin" ]]; then
  mem=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  cpus=$(sysctl -n hw.ncpu)
else
  mem=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1048576 ))
  cpus=$(nproc)
fi
[[ $mem -ge 16 ]] && ok "${mem}GB RAM" || warn "${mem}GB RAM — 24GB recommended, Neo4j and Prometheus are hungry"
[[ $cpus -ge 4 ]] && ok "${cpus} CPUs" || warn "${cpus} CPUs — 8 recommended"
disk=$(df -Pg . | awk 'NR==2 {print $4}')
[[ ${disk:-0} -ge 50 ]] && ok "${disk}GB disk free" || warn "${disk}GB disk free — 200GB recommended"

echo "Configuration"
if [[ ! -f .env ]]; then
  bad ".env missing — run: cp .env.example .env  (or ./scripts/gen-secrets.sh)"
else
  ok ".env present"
  # Anchored to real assignments: the .env header comments mention CHANGEME
  # too, and counting those reports a wrong number with nonsense names.
  # [A-Z0-9_] not [A-Z_] — SNMPV3_AUTH contains a digit.
  if grep -qE '^[A-Z0-9_]+=CHANGEME' .env; then
    bad "$(grep -cE '^[A-Z0-9_]+=CHANGEME' .env) value(s) still CHANGEME: $(grep -E '^[A-Z0-9_]+=CHANGEME' .env | cut -d= -f1 | tr '\n' ' ')"
  else
    ok "no CHANGEME values left"
  fi
  # The single most common misconfiguration: pointing devices at a container
  # IP or loopback, so syslog and flow silently never arrive.
  ip=$(grep -E '^SYSLOG_COLLECTOR_IP=' .env | cut -d= -f2)
  case "$ip" in
    "")
      bad "SYSLOG_COLLECTOR_IP is empty" ;;
    127.*|localhost|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
      bad "SYSLOG_COLLECTOR_IP=$ip is a loopback or docker-internal address — devices cannot reach it. Use the VM's routable IP." ;;
    *)
      if [[ ! $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
        bad "SYSLOG_COLLECTOR_IP=$ip is not an IPv4 address. Devices need a literal IP, not a name."
      elif ! ip -4 addr show scope global 2>/dev/null | grep -q "$ip" \
        && ! ifconfig 2>/dev/null | grep -q "inet $ip"; then
        warn "SYSLOG_COLLECTOR_IP=$ip is not an address on this host — correct only if traffic is forwarded here"
      else
        ok "SYSLOG_COLLECTOR_IP=$ip (found on a local interface)"
      fi ;;
  esac
fi

# Devices push syslog and flow over UDP. An undersized kernel receive buffer
# drops datagrams silently — no error, no log line, no retransmit — and the
# application's request for a bigger one is clamped without complaint.
echo "Kernel (UDP receive buffers)"
if [[ "$(uname)" == "Linux" ]]; then
  for spec in "net.core.rmem_max:8388608" "net.core.rmem_default:1048576"; do
    key=${spec%%:*}; want=${spec##*:}
    have=$(sysctl -n "$key" 2>/dev/null || echo 0)
    if [[ ${have:-0} -ge $want ]]; then
      ok "$key = $have"
    else
      bad "$key = $have, needs >= $want — syslog and flow will be dropped silently. Fix: sudo python3 install.py --fix-sysctl"
    fi
  done
else
  warn "not Linux — UDP buffer limits not checked (they apply on the deployment host)"
fi

echo "Ports"
# Check the ports actually configured in .env, not a hardcoded list — the
# whole point of making them configurable is to move off a busy one.
get() { grep -E "^$1=" .env 2>/dev/null | cut -d= -f2 | cut -d'#' -f1 | tr -d ' \t' ; }
declare -a busy=()
for spec in "GRAFANA_PORT:3000:Grafana" "INFRAHUB_PORT:8000:Infrahub" \
            "PROMETHEUS_PORT:9090:Prometheus" "ALERTMANAGER_PORT:9093:Alertmanager" \
            "LOKI_PORT:3100:Loki" "AUTOMATION_PORT:8100:Automation API" \
            "SYSLOG_PORT:514:syslog" "NETFLOW_PORT:2055:NetFlow" "IPFIX_PORT:4739:IPFIX"; do
  var=${spec%%:*}; rest=${spec#*:}; def=${rest%%:*}; label=${rest#*:}
  port=$(get "$var"); port=${port:-$def}
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iUDP:"$port" >/dev/null 2>&1; then
    bad "port $port ($label) is in use — set $var to a free port in .env"
    busy+=("$port")
  fi
done
[[ ${#busy[@]} -eq 0 ]] && ok "all configured ports are free"

echo "Compose"
if docker compose config -q 2>/dev/null; then ok "compose files valid"; else bad "docker compose config failed"; fi

echo
if [[ $fail -eq 0 ]]; then
  echo "Preflight passed — run: make up"
else
  echo "Preflight FAILED — fix the above, then re-run." >&2
  exit 1
fi
