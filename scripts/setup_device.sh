#!/bin/bash

# Raspberry Pi setup script for the Embedded Network Performance Monitor.
# Installation and configuration steps will be added as the project develops.

set -e

echo "Updating package lists..."
sudo apt update

echo "Installing required system packages..."
sudo apt install -y snmp

echo "Checking Python 3..."
python3 --version

echo "Checking SNMP client..."
snmpget --version

echo "Setup complete."
