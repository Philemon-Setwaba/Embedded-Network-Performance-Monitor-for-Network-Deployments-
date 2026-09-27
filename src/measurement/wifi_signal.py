"""
src/measurement/wifi_signal.py

Reads Wi-Fi signal quality of the Pi's wireless connection using the
system `iw` command (`iw dev <interface> link`).

Ping and iperf3 show THAT the link is slow or losing packets; the Wi-Fi
signal shows whether the Pi's own wireless connection is the cause
(e.g. weak signal, or a switch to a different access point).

Usage:
    python3 src/measurement/wifi_signal.py           # reads wlan0
    python3 src/measurement/wifi_signal.py wlan1     # reads another interface
"""

import argparse
import dataclasses
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

DEFAULT_INTERFACE = "wlan0"

# Safety limit so a stuck command cannot hang forever.
RUN_TIMEOUT_S = 5.0

# Patterns for the lines of `iw dev wlan0 link`, for example:
#   Connected to 74:11:b2:2b:c3:8e (on wlan0)
#   SSID: eduroam
#   freq: 5580.0
#   signal: -59 dBm
#   rx bitrate: 200.0 MBit/s
#   tx bitrate: 24.0 MBit/s
BSSID_PATTERN = re.compile(r"Connected to ([0-9a-fA-F:]{17})")
SSID_PATTERN = re.compile(r"^\s*SSID: (.*)$", re.MULTILINE)
FREQ_PATTERN = re.compile(r"freq: ([\d.]+)")
SIGNAL_PATTERN = re.compile(r"signal: (-?\d+) dBm")
RX_BITRATE_PATTERN = re.compile(r"rx bitrate: ([\d.]+) MBit/s")
TX_BITRATE_PATTERN = re.compile(r"tx bitrate: ([\d.]+) MBit/s")


@dataclasses.dataclass
class WifiResult:
    """Stores one reading of the Wi-Fi connection."""

    timestamp: str
    interface: str
    connected: bool

    # The fields below are None when not connected.
    ssid: Optional[str] = None             # network name
    bssid: Optional[str] = None            # MAC address of the access point
    freq_mhz: Optional[float] = None       # 2.4 GHz band or 5 GHz band
    signal_dbm: Optional[int] = None       # closer to 0 = stronger
    rx_bitrate_mbps: Optional[float] = None  # radio rate, not real throughput
    tx_bitrate_mbps: Optional[float] = None  # radio rate, not real throughput

    # Error message if `iw` could not be run or failed, e.g. an unknown
    # interface. None when the reading worked (connected or not).
    error_detail: Optional[str] = None


def _find(pattern: re.Pattern, text: str, convert=str):
    """Return the first match of pattern in text, converted, or None."""
    match = pattern.search(text)
    return convert(match.group(1)) if match else None


def read_wifi(interface: str = DEFAULT_INTERFACE) -> WifiResult:
    """Read the current Wi-Fi connection details for one interface."""

    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            ["iw", "dev", interface, "link"],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # iw did not finish, or is not installed
        return WifiResult(timestamp, interface, False, error_detail=str(exc))

    if proc.returncode != 0:
        # e.g. "command failed: No such device (-19)" for a wrong interface
        detail = (proc.stderr or proc.stdout).strip() or None
        return WifiResult(timestamp, interface, False, error_detail=detail)

    output = proc.stdout

    # iw prints just "Not connected." when there is no Wi-Fi connection
    if "Not connected" in output:
        return WifiResult(timestamp, interface, False)

    return WifiResult(
        timestamp=timestamp,
        interface=interface,
        connected=True,
        ssid=_find(SSID_PATTERN, output),
        bssid=_find(BSSID_PATTERN, output),
        freq_mhz=_find(FREQ_PATTERN, output, float),
        signal_dbm=_find(SIGNAL_PATTERN, output, int),
        rx_bitrate_mbps=_find(RX_BITRATE_PATTERN, output, float),
        tx_bitrate_mbps=_find(TX_BITRATE_PATTERN, output, float),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read Wi-Fi signal quality.")
    parser.add_argument(
        "interface",
        nargs="?",
        default=DEFAULT_INTERFACE,
        help=f"wireless interface (default: {DEFAULT_INTERFACE})",
    )
    args = parser.parse_args()

    print(read_wifi(args.interface))