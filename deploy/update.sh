#!/bin/bash
# Pull the latest code and restart. Run as root on the box:  sudo bash deploy/update.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../.deploy_user" 2>/dev/null || echo runproof)}"
cd "$REPO_DIR"

echo "==> before: $(git rev-parse --short HEAD)"
git fetch --all --tags --quiet
git reset --hard origin/main --quiet        # .env and spe_state/ are gitignored and survive
echo "==> after:  $(git rev-parse --short HEAD)"

chown -R "$APP_USER:$APP_USER" "$REPO_DIR"
chmod 600 "$REPO_DIR/.env"
systemctl restart sos-kernel spe
sleep 4
echo "==> status: spe=$(systemctl is-active spe) kernel=$(systemctl is-active sos-kernel)"
if ! sudo -u "$APP_USER" test -r "$REPO_DIR/tests/test_spe.py"; then
  echo "==> WARNING: $APP_USER cannot read $REPO_DIR — the service cannot run from here."
  echo "    Move the checkout to a readable path and re-run deploy/install.sh."
  exit 1
fi
echo "==> suite (as $APP_USER):"
sudo -u "$APP_USER" env SPE_ENV_FILE="$REPO_DIR/.env" python3 "$REPO_DIR/tests/test_spe.py" 2>&1 | tail -8
