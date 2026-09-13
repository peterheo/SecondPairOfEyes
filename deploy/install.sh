#!/bin/bash
# Install Second Pair of Eyes from a git checkout. Idempotent: safe to re-run.
#
#   sudo bash deploy/install.sh [public-ip]
#
# Expects /opt/spe/.env to exist (see .env.example). Preserves .env and spe_state/.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "FATAL: run this with sudo — it writes systemd units and restarts services."
  echo "       sudo bash ${BASH_SOURCE[0]}"
  exit 1
fi
# root running git in a checkout owned by another user trips git's ownership guard
git config --global --add safe.directory "$REPO_DIR" 2>/dev/null || true
# Remember the service account so update.sh cannot silently pick a different one.
APP_USER="${APP_USER:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../.deploy_user" 2>/dev/null || echo runproof)}"
IP="${1:-}"

if [ -z "$IP" ]; then
  IP=$(curl -s --max-time 5 -H "Authorization: Bearer Oracle" \
        http://169.254.169.254/opc/v2/vnics/ | grep -o '"publicIp"[^,]*' | head -1 | cut -d'"' -f4 || true)
fi
[ -z "$IP" ] && IP=$(curl -s --max-time 8 https://api.ipify.org || true)
echo "$IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || { echo "FATAL: could not determine public IP; pass it: sudo bash deploy/install.sh <ip>"; exit 1; }
echo "==> repo: $REPO_DIR   public ip: $IP"

echo "==> dependencies"
if command -v dnf >/dev/null; then
  dnf install -y python3 nodejs git >/dev/null
elif command -v apt-get >/dev/null; then
  apt-get update -qq && apt-get install -y python3 nodejs git >/dev/null
else
  echo "FATAL: no dnf or apt-get"; exit 1
fi

echo "==> service account: $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd -r -m -d "/var/lib/$APP_USER" -s /usr/sbin/nologin "$APP_USER"
printf '%s' "$APP_USER" > "$REPO_DIR/.deploy_user"

# The service account must actually be able to read the checkout. A clone under
# /root is mode 700, so the units start and then fail on every file access.
if ! sudo -u "$APP_USER" test -r "$REPO_DIR/src/spe.py"; then
  echo "FATAL: $APP_USER cannot read $REPO_DIR (a clone under /root is mode 700)."
  echo "       Move the checkout somewhere readable, e.g.:"
  echo "         sudo mv $REPO_DIR /opt/spe-git && cd /opt/spe-git && sudo bash deploy/install.sh"
  exit 1
fi
mkdir -p "$REPO_DIR/spe_state"

if [ ! -f "$REPO_DIR/.env" ]; then
  echo "FATAL: $REPO_DIR/.env is missing."
  echo "       cp .env.example .env && chmod 600 .env, then fill in SHAREDOS_KEY and NIM_KEY."
  exit 1
fi
chmod 600 "$REPO_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$REPO_DIR"

echo "==> systemd units"
cat > /etc/systemd/system/sos-kernel.service <<UNIT
[Unit]
Description=SharedOS kernel sidecar (Second Pair of Eyes)
After=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$REPO_DIR/src
EnvironmentFile=$REPO_DIR/.env
ExecStart=/usr/bin/node $REPO_DIR/src/kernel_sidecar.mjs
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/spe.service <<UNIT
[Unit]
Description=Second Pair of Eyes
After=network-online.target sos-kernel.service

[Service]
User=$APP_USER
WorkingDirectory=$REPO_DIR/src
EnvironmentFile=$REPO_DIR/.env
Environment=SPE_ENV_FILE=$REPO_DIR/.env
ExecStart=/usr/bin/python3 $REPO_DIR/src/spe.py 8400
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now sos-kernel
systemctl enable --now spe
systemctl restart sos-kernel spe     # enable --now does nothing if already running
sleep 4

if command -v caddy >/dev/null; then
  echo "==> caddy route"
  cat > /etc/caddy/Caddyfile <<CADDY
${IP}.nip.io {
    handle /spe/* {
        uri strip_prefix /spe
        reverse_proxy 127.0.0.1:8400
    }
    handle /r/* {
        reverse_proxy 127.0.0.1:8400
    }
    request_body {
        max_size 8MB
    }
}
CADDY
  caddy validate --config /etc/caddy/Caddyfile >/dev/null && systemctl restart caddy
else
  echo "==> caddy not installed; skipping TLS front end"
fi

echo "==> status"
echo "    spe=$(systemctl is-active spe)  kernel=$(systemctl is-active sos-kernel)  caddy=$(systemctl is-active caddy 2>/dev/null || echo n/a)"
curl -s --max-time 10 http://127.0.0.1:8400/v1/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('    reviewer:', d['reviewer']['autonomous'], '| kernel:', d['sharedos']['kernel_reachable'], '| audit:', d['sharedos']['audit_enabled'])" || true
echo "==> public base: https://${IP}.nip.io/spe"
