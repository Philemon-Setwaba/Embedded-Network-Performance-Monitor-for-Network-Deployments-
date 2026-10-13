#!/usr/bin/env bash
# infrastructure/netmon/install.sh
#
# Installs the "netmon" systemd service, which runs src/scheduler.py
# on boot and restarts it if it crashes.
#
# Usage (from the project folder):
#   sudo bash infrastructure/netmon/install.sh
#
# Safe to run again (e.g. after changing netmon.service).

# Stop at the first error, on undefined variables, and on failed pipes.
set -euo pipefail

# Copying into /etc needs sudo.
if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# The user who typed "sudo" (not root) - the service runs as this user.
RUN_USER="${SUDO_USER:?run with sudo from your normal user, not as root}"

# The project folder = two folders up from this script
# (infrastructure/netmon/install.sh -> project root).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

SRC_UNIT="$SCRIPT_DIR/netmon.service"
DST_UNIT="/etc/systemd/system/netmon.service"

echo "Installing netmon service"
echo "  user:   $RUN_USER"
echo "  folder: $REPO_DIR"

# Fill in __USER__ and __REPO__ and write the service file.
sed -e "s|__USER__|$RUN_USER|" -e "s|__REPO__|$REPO_DIR|" "$SRC_UNIT" > "$DST_UNIT"
chmod 644 "$DST_UNIT"

# Tell systemd about the new/changed file, start on boot, (re)start now.
systemctl daemon-reload
systemctl enable netmon
systemctl restart netmon

sleep 3
if systemctl is-active --quiet netmon; then
  echo ""
  echo "✓ netmon is running."
  echo "  Live output:  journalctl -u netmon -f"
  echo "  Stop:         sudo systemctl stop netmon"
else
  echo "✗ netmon did not start."
  echo "  Check:  journalctl -u netmon -n 50"
  exit 1
fi
