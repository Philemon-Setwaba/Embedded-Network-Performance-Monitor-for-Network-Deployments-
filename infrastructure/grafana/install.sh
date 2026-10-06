#!/usr/bin/env bash
# infrastructure/grafana/install.sh
#
# Installs the Grafana provisioning files from this repo:
#   - the VictoriaMetrics data source  (provisioning/datasource.yaml)
#   - the dashboard loader             (provisioning/dashboards.yaml)
#   - any dashboards                   (dashboards/*.json)
# and restarts Grafana so it picks them up.
#
# Grafana itself must already be installed (the .deb from grafana.com).
#
# Usage (from the project folder):
#   sudo bash infrastructure/grafana/install.sh
#
# Safe to run again: it just copies the latest files over the old ones.
# Run it again every time you update a dashboard .json in the repo.

# Stop at the first error, on undefined variables, and on failed pipes.
set -euo pipefail

# =============================================================================
# ROOT CHECK - copying into /etc and /var/lib needs sudo
# =============================================================================

if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo"
  exit 1
fi

# =============================================================================
# PATHS
# =============================================================================

# The folder this script is in (infrastructure/grafana/), wherever it is
# called from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Our files in the repo.
SRC_DATASOURCE="${SCRIPT_DIR}/provisioning/datasource.yaml"
SRC_DASHBOARDS_YAML="${SCRIPT_DIR}/provisioning/dashboards.yaml"
SRC_DASHBOARD_DIR="${SCRIPT_DIR}/dashboards"

# Where Grafana looks for provisioning files (set up by the Grafana .deb).
DST_DATASOURCE="/etc/grafana/provisioning/datasources/victoriametrics.yaml"
DST_DASHBOARDS_YAML="/etc/grafana/provisioning/dashboards/network-monitor.yaml"

# Where Grafana loads dashboard .json files from
# (must match "path" in dashboards.yaml).
DST_DASHBOARD_DIR="/var/lib/grafana/dashboards/network-monitor"

# =============================================================================
# COPY THE PROVISIONING FILES
# =============================================================================

echo "Installing data source..."
# install -D : create the folder if it is missing
# -m 640     : owner can read/write, group can read, others nothing
# -o root -g grafana : owned by root, readable by the grafana group
install -D -m 640 -o root -g grafana "$SRC_DATASOURCE" "$DST_DATASOURCE"

echo "Installing dashboard loader..."
install -D -m 640 -o root -g grafana "$SRC_DASHBOARDS_YAML" "$DST_DASHBOARDS_YAML"

# =============================================================================
# COPY THE DASHBOARDS
# =============================================================================

echo "Installing dashboards..."
# Create the folder Grafana reads dashboards from, owned by grafana.
install -d -m 755 -o grafana -g grafana "$DST_DASHBOARD_DIR"

# nullglob: if there are no .json files yet, the loop simply runs 0 times
# (instead of trying to copy a file literally called "*.json").
shopt -s nullglob
json_files=("${SRC_DASHBOARD_DIR}"/*.json)

if [[ ${#json_files[@]} -eq 0 ]]; then
  echo "  (no dashboards in ${SRC_DASHBOARD_DIR} yet - that's fine)"
fi

for file in "${json_files[@]}"; do
  echo "  $(basename "$file")"
  install -m 644 -o grafana -g grafana "$file" "$DST_DASHBOARD_DIR/"
done

# =============================================================================
# RESTART GRAFANA SO IT READS THE NEW FILES
# =============================================================================

echo "Restarting Grafana..."
systemctl restart grafana-server

# Give Grafana a few seconds to start up.
sleep 5

if systemctl is-active --quiet grafana-server; then
  echo ""
  echo "✓ Grafana is running with the provisioning files."
  echo "  Open: http://localhost:3000"
  echo "  Check: Connections -> Data sources -> VictoriaMetrics"
else
  echo "✗ Grafana did not start."
  echo "  Check logs: journalctl -u grafana-server -n 50"
  exit 1
fi