#!/usr/bin/env bash
#
# scripts/run_iperf_server.sh
#
# Runs on the "target device" - your machine, standing in for the
# always-on reference server on the far side of the backhaul link.
# The Pi (running IperfMeasurer as the client) tests against this.
#
# Usage:
#   ./scripts/run_iperf_server.sh
#   (always runs on port 5201, iperf3's standard default - not configurable)



# Exit on errors, unset variables, and failed commands in pipelines
set -euo pipefail


# Fixed port - always 5201, not configurable
PORT=5201

echo "Starting iperf3 server on port ${PORT}..."
echo "Leave this running. Point the Pi's IperfMeasurer at this machine's"
echo "IP address on port ${PORT}. Ctrl+C to stop."
echo ""


# Start iperf3 in server mode on the fixed port
iperf3 -s -p "$PORT"