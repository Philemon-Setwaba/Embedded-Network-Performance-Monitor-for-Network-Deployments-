#!/usr/bin/env bash
# infrastructure/vmagent/set_receiver.sh
#
# Points the name of the remote receiver (config.json "remote.host",
# e.g. netmon-receiver) at its current IP (config.json "remote.ip"),
# by writing ONE line in /etc/hosts:
#     100.x.y.z netmon-receiver
# The same line is written to the cloud-init template
# (/etc/cloud/templates/hosts.debian.tmpl), because Raspberry Pi OS rebuilds
# /etc/hosts from it at every boot and would otherwise remove the entry.
#
# Use it when the laptop's IP changes:
#   1. edit "ip" under "remote" in config.json
#   2. sudo bash infrastructure/vmagent/set_receiver.sh
# vmagent does not need a restart: the URL (the name) stays the same, so
# data waiting in its buffer is sent to the new IP.
#
# install.sh also runs this script.

# Stop at the first error, on undefined variables, and on failed pipes.
set -euo pipefail

# Editing /etc/hosts needs sudo.
if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# The project folder = two folders up from this script.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_DIR/config.json"

# Read host and ip from config.json, and check them.
# (python3 is already on the Pi; ipaddress rejects a mistyped IP.)
read -r HOST IP < <(python3 - "$CONFIG" << 'EOF'
import ipaddress, json, re, sys
remote = json.load(open(sys.argv[1]))["remote"]
host, ip = remote["host"], remote["ip"]
ipaddress.IPv4Address(ip)                       # error if not a valid IPv4
if not re.fullmatch(r"[a-z0-9-]+", host):        # simple name only
    sys.exit("remote.host must be letters, digits or '-' only")
print(host, ip)
EOF
)

# Remove any old line for this name, then add the new one.
# (Only lines that END with the name are removed.)
sed -i "/[[:space:]]${HOST}\$/d" /etc/hosts
echo "${IP} ${HOST}" >> /etc/hosts

# Same line in the cloud-init template, so it survives a reboot.
TEMPLATE=/etc/cloud/templates/hosts.debian.tmpl
if [[ -f "$TEMPLATE" ]]; then
  sed -i "/[[:space:]]${HOST}\$/d" "$TEMPLATE"
  echo "${IP} ${HOST}" >> "$TEMPLATE"
  echo "Also written to $TEMPLATE"
fi

echo "/etc/hosts now has:"
grep -E "[[:space:]]${HOST}\$" /etc/hosts

# Show what the Pi now finds for the name.
echo "The Pi resolves ${HOST} to: $(getent hosts "$HOST" | awk '{print $1}')"