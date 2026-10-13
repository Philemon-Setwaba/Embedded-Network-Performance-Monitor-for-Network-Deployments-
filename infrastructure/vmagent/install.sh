#!/usr/bin/env bash
# infrastructure/vmagent/install.sh
#
# Installs the "vmagent" systemd service (store-and-forward).
#   1. checks vmagent is installed at /usr/local/bin/vmagent
#   2. points the receiver name at its IP (runs set_receiver.sh)
#   3. writes /etc/systemd/system/vmagent.service from vmagent.service
#   4. starts it now and on every boot
#
# Usage (from the project folder):
#   sudo bash infrastructure/vmagent/install.sh
#
# Safe to run again (e.g. after changing vmagent.service).
# Only the IP changed? Run set_receiver.sh instead (no restart needed).

# Stop at the first error, on undefined variables, and on failed pipes.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# The user who typed "sudo" (not root) - the service runs as this user.
RUN_USER="${SUDO_USER:?run with sudo from your normal user, not as root}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_DIR/config.json"
SRC_UNIT="$SCRIPT_DIR/vmagent.service"
DST_UNIT="/etc/systemd/system/vmagent.service"

# 1. vmagent must already be installed (downloaded from the v1.153.0 release).
if [[ ! -x /usr/local/bin/vmagent ]]; then
  echo "Error: /usr/local/bin/vmagent not found - install vmagent first"
  exit 1
fi
echo "vmagent version: $(/usr/local/bin/vmagent -version 2>/dev/null | tail -1)"

# 2. Name -> IP in /etc/hosts.
bash "$SCRIPT_DIR/set_receiver.sh"

# 3. Read host and port from config.json and fill in the service file.
read -r HOST PORT < <(python3 -c '
import json, sys
r = json.load(open(sys.argv[1]))["remote"]
print(r["host"], int(r["port"]))
' "$CONFIG")

echo "Installing vmagent service"
echo "  user:       $RUN_USER"
echo "  remote URL: http://$HOST:$PORT/api/v1/write"

sed -e "s|__USER__|$RUN_USER|" -e "s|__HOST__|$HOST|" -e "s|__PORT__|$PORT|" \
    "$SRC_UNIT" > "$DST_UNIT"
chmod 644 "$DST_UNIT"

# 4. Tell systemd, start on boot, (re)start now.
systemctl daemon-reload
systemctl enable vmagent
systemctl restart vmagent

sleep 3
if systemctl is-active --quiet vmagent \
   && curl -s -o /dev/null http://127.0.0.1:8429/metrics; then
  echo ""
  echo "✓ vmagent is running and answers on 127.0.0.1:8429."
  echo "  Logs:          journalctl -u vmagent -f"
  echo "  Buffer folder: /var/lib/vmagent"
else
  echo "✗ vmagent did not start."
  echo "  Check:  journalctl -u vmagent -n 50"
  exit 1
fi