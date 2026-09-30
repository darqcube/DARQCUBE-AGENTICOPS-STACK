#!/usr/bin/env bash
# Prepare a fresh Ubuntu (or Debian) host for the stack. Run once, before
# install.py:
#
#   ./scripts/prepare-ubuntu.sh
#
# Installs what install.py cannot install for itself — OS packages and Docker —
# and nothing else. It needs sudo, which is why it is a separate, explicit step
# rather than something install.py does behind your back.
#
# Safe to re-run: every step is skipped when it is already done.
set -euo pipefail

# Keep in step with docs/install/01-prerequisites.md — a test compares them.
PACKAGES=(git make jq python3-venv curl)
COMPOSE_MIN="2.20"     # compose.yaml uses `include:`

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
do_()  { printf '  \033[36m..\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }

# --- where are we -----------------------------------------------------------
[[ -r /etc/os-release ]] && . /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" ubuntu "*|*" debian "*) ok "${PRETTY_NAME:-$ID}" ;;
  *) fail "this script is for Ubuntu/Debian — on anything else follow docs/install/01-prerequisites.md" ;;
esac

if [[ $(id -u) -eq 0 ]]; then
  SUDO=""
else
  command -v sudo >/dev/null || fail "sudo not found — run as root, or install sudo first"
  SUDO="sudo"
fi

# --- 1. OS packages ---------------------------------------------------------
echo "[1/4] OS packages"
missing=()
for p in "${PACKAGES[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done
if ((${#missing[@]})); then
  do_ "installing: ${missing[*]}"
  $SUDO apt-get update -q
  $SUDO apt-get install -y -q "${missing[@]}"
fi
ok "${PACKAGES[*]}"

# --- 2. Docker with the Compose v2 plugin -----------------------------------
echo "[2/4] Docker"
if ! command -v docker >/dev/null; then
  do_ "installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
ok "$(docker --version)"

# Ubuntu's own docker.io package ships without the Compose plugin; get.docker.com
# installs it. Neither `docker compose` nor its version needs the daemon.
compose=$(docker compose version --short 2>/dev/null || true)
[[ -n $compose ]] || fail "docker compose plugin missing — sudo apt install -y docker-compose-plugin (or reinstall via get.docker.com)"
if [[ $(printf '%s\n%s\n' "$COMPOSE_MIN" "${compose#v}" | sort -V | head -1) != "$COMPOSE_MIN" ]]; then
  fail "docker compose $compose is older than $COMPOSE_MIN — compose.yaml needs include:"
fi
ok "docker compose $compose"

# --- 3. Run Docker without sudo --------------------------------------------
echo "[3/4] docker group"
relogin=0
if [[ $(id -u) -ne 0 ]]; then
  if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    ok "$USER is in the docker group"
  else
    do_ "adding $USER to the docker group"
    $SUDO usermod -aG docker "$USER"
    relogin=1
  fi
fi

# --- 4. Start containers only after the clock is right ---------------------
# A VM can boot with a wrong clock — VMware hands a guest the host's LOCAL time
# at power-on — and NTP corrects it seconds later. Anything started in between
# stamps data with that wrong time. Prometheus is the casualty: samples written
# hours in the future make every later, correct sample "out of bounds", so all
# metrics are rejected until its data is wiped. Make Docker wait for the first
# NTP sync instead.
echo "[4/4] Docker waits for NTP"
if [[ ! -d /run/systemd/system ]]; then
  ok "no systemd here — skipped (nothing to order Docker against)"
else
  $SUDO timedatectl set-ntp true
  if systemctl cat systemd-time-wait-sync.service >/dev/null 2>&1; then
    wait_unit=systemd-time-wait-sync.service        # timesyncd — Ubuntu's default
  elif systemctl cat chrony-wait.service >/dev/null 2>&1; then
    wait_unit=chrony-wait.service                   # hosts running chrony
  else
    wait_unit=""
  fi
  if [[ -z $wait_unit ]]; then
    printf '  \033[33mwarn\033[0m  no NTP wait service found — Docker may start before the clock is synced\n'
  else
    systemctl is-enabled "$wait_unit" >/dev/null 2>&1 || $SUDO systemctl enable "$wait_unit" >/dev/null 2>&1
    dropin=/etc/systemd/system/docker.service.d/wait-for-time.conf
    want=$'[Unit]\nAfter=time-sync.target\nWants=time-sync.target\n'
    if [[ "$(cat "$dropin" 2>/dev/null)"$'\n' != "$want" ]]; then
      do_ "Docker will start after the first NTP sync"
      $SUDO mkdir -p "$(dirname "$dropin")"
      printf '%s' "$want" | $SUDO tee "$dropin" >/dev/null
      $SUDO systemctl daemon-reload
    fi
    ok "Docker starts after time-sync.target ($wait_unit)"
  fi
fi

# --- next ------------------------------------------------------------------
echo
echo "Host ready. Next:"
((relogin)) && echo "  0. LOG OUT AND BACK IN — group membership is read at login"
cat <<'EOF'
  1. sudo python3 install.py --fix-sysctl       # UDP buffers, once per host
  2. cp site.example.yml site.yml && chmod 600 site.yml
     ${EDITOR:-nano} site.yml                    # describe this deployment
  3. python3 install.py
EOF
