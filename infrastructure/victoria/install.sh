#!/usr/bin/env bash
# install.sh — Downloads and installs VictoriaMetrics on a Raspberry Pi 5
# Usage: sudo ./install.sh

# Stop the script immediately if any command fails, if an undefined variable
# is used, or if any command in a pipe fails. Prevents silent broken installs.
set -euo pipefail

# =============================================================================
# VARIABLES — change these if you upgrade or move to a different machine
# =============================================================================

# The exact release version to install (from GitHub releases page)
VERSION="v1.153.0"

# CPU architecture: arm64 = Pi 5 with 64-bit OS
ARCH="arm64"

# The Linux user that will OWN and RUN the VictoriaMetrics process
# (not root — security best practice)
VM_USER="victoriametrics"

# Where metric data will be stored on the Pi's SD card
VM_DATA_DIR="/var/lib/victoriametrics"

# Where the program binary will be installed
# /usr/local/bin is the standard location for manually installed programs
VM_BIN="/usr/local/bin/victoria-metrics"

# =============================================================================
# ROOT CHECK — this script needs sudo because it installs system files
# =============================================================================

# $EUID is the current user's ID. Root is always 0.
if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# =============================================================================
# DOWNLOAD — fetch the binary from GitHub releases
# =============================================================================

# Build the filename using our variables, e.g:
# victoria-metrics-linux-arm64-v1.153.0.tar.gz
TARBALL="victoria-metrics-linux-${ARCH}-${VERSION}.tar.gz"

# Build the full GitHub download URL
URL="https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/${VERSION}/${TARBALL}"

# Create a temporary folder for the download — gets cleaned up at the end
TMP_DIR=$(mktemp -d)

echo "Downloading VictoriaMetrics ${VERSION} for ${ARCH}..."

# curl flags explained:
#   -f  fail with error if download fails (don't save an HTML error page)
#   -s  silent (no progress bar)
#   -S  but still show errors if something goes wrong
#   -L  follow redirects (GitHub redirects to their CDN)
#   -o  save to this specific file path
curl -fsSL -o "${TMP_DIR}/${TARBALL}" "$URL"

echo "Extracting..."

# tar flags:
#   -x  extract
#   -z  the file is gzip compressed (.gz)
#   -f  the file to extract
#   -C  extract into this directory (our temp folder)
tar -xzf "${TMP_DIR}/${TARBALL}" -C "$TMP_DIR"

# =============================================================================
# INSTALL BINARY — copy the program to its permanent location
# =============================================================================

echo "Installing binary..."

# 'install' copies a file AND sets permissions in one step
# -m 755 means: owner can read/write/execute, everyone else can read/execute
# This is the standard permission for a program binary
install -m 755 "${TMP_DIR}/victoria-metrics-prod" "$VM_BIN"

# =============================================================================
# SYSTEM USER — create a dedicated user to run the process
# =============================================================================

# Check if the user already exists before trying to create it
# (so running this script twice doesn't error out)
# 'id' looks up a user by name; &>/dev/null hides the output
if ! id "$VM_USER" &>/dev/null; then
  echo "Creating system user: ${VM_USER}..."

  # --system        = creates a system account, not a real login user
  # --no-create-home = no /home/victoriametrics folder needed
  # --shell /bin/false = nobody can log in as this user
  useradd --system --no-create-home --shell /bin/false "$VM_USER"
fi

# =============================================================================
# DATA DIRECTORY — where metrics will be stored on the SD card
# =============================================================================

echo "Creating data directory..."

# install -d creates a directory (like mkdir but also sets owner + permissions)
# -o and -g set the owner and group to our victoriametrics user
# -m 750 means: owner has full access, group can read, others get nothing
install -d -o "$VM_USER" -g "$VM_USER" -m 750 "$VM_DATA_DIR"

# =============================================================================
# SYSTEMD SERVICE — register VM as a background service that starts on boot
# =============================================================================

# Find the folder this script lives in, so we can find victoria.service
# next to it — works no matter where you call the script from
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# The .service file we wrote, sitting next to this script in the repo
SERVICE_SRC="${SCRIPT_DIR}/victoria.service"

# systemd reads service files from /etc/systemd/system/
SERVICE_DST="/etc/systemd/system/victoria.service"

echo "Installing systemd service..."
cp "$SERVICE_SRC" "$SERVICE_DST"

# Service files should be readable by everyone, writable only by root
chmod 644 "$SERVICE_DST"

# Tell systemd to re-read all service files (picks up our new one)
systemctl daemon-reload

# Register VM to start automatically every time the Pi boots
systemctl enable victoria

# Start it right now (no reboot needed)
systemctl start victoria

# =============================================================================
# VERIFY — confirm the service actually came up
# =============================================================================

echo "Waiting for startup..."
sleep 3  # give the process a moment to initialise

# systemctl is-active returns exit code 0 if running, non-zero if not
if systemctl is-active --quiet victoria; then
  echo ""
  echo "✓ VictoriaMetrics is running!"
  echo "  Endpoint : http://localhost:8428"
  echo "  Data dir : ${VM_DATA_DIR}"
  echo "  Logs     : journalctl -u victoria -f"
else
  echo "✗ Service failed to start."
  echo "  Check logs: journalctl -u victoria -n 30"
  exit 1
fi

# =============================================================================
# CLEANUP — remove the temp download folder
# =============================================================================

# -r removes directories recursively, -f forces without asking
rm -rf "$TMP_DIR"
echo "Done."