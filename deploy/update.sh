#!/bin/bash
# Pull the latest code and restart. Run as root on the box:  sudo bash deploy/update.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${APP_USER:-runproof}"
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
echo "==> suite:"
sudo -u "$APP_USER" env SPE_ENV_FILE="$REPO_DIR/.env" python3 "$REPO_DIR/tests/test_spe.py" 2>&1 | tail -8
