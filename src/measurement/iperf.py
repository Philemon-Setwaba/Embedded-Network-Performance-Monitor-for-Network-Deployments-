"""
src/measurement/iperf.py

Active measurement: throughput, jitter and packet loss using iperf3 (FR-2).

The Pi runs the iperf3 CLIENT; an always-on iperf3 SERVER sits on the far
side of the link (see scripts/run_iperf_server.sh). Three tests are
available, each run separately so the scheduler can choose which to run:

  - TCP upload   (Pi -> server): achievable throughput, retransmissions
  - TCP download (server -> Pi): same, using iperf3's reverse mode (-R)
  - UDP          (Pi -> server): jitter and packet loss at a fixed, low rate

Tests must run one after another, never at the same time, and not while
other active measurements run, because they compete for the same link.

Results are read from iperf3's JSON output (-J) rather than its text
tables, so every value is read by name instead of by pattern matching.

The server address is read from config.json in the repository root, so
it can be changed without editing code.

This file is named iperf.py rather than iperf3.py to avoid clashing with
the `iperf3` package on PyPI.
"""

import argparse
import dataclasses
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# config.json in the repository root (src/measurement/iperf.py -> ../../)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"

DEFAULT_PORT = 5201

# Test length is normally chosen by the scheduler; these are defaults.
DEFAULT_DURATION_S = 10

# TCP starts slowly and speeds up; the first seconds are left out of the
# result so short tests do not underestimate throughput.
DEFAULT_OMIT_S = 2

# UDP send rate: about 10 % of a 5 Mbps community uplink (e.g. iNethi),
# enough to measure jitter and loss without disrupting users.
DEFAULT_UDP_RATE = "500K"

# Extra time allowed for connecting and finishing before Python stops
# iperf3 (the test itself takes duration + omit seconds).
RUN_TIMEOUT_MARGIN_S = 15

# Pause between tests: the server needs a moment to finish one test
# before it accepts the next, otherwise it replies "server is busy".
PAUSE_BETWEEN_TESTS_S = 2

TEST_TCP_UPLOAD = "tcp_upload"
TEST_TCP_DOWNLOAD = "tcp_download"
TEST_UDP = "udp"


@dataclasses.dataclass
class IperfResult:
    """Stores the result of one iperf3 test."""

    timestamp: str
    test: str                   # tcp_upload, tcp_download or udp
    server: str
    port: int
    duration_s: int
    success: bool

    # Throughput measured at the receiver, in Mbit/s (all tests).
    throughput_mbps: Optional[float] = None

    # TCP only: packets the sender had to resend.
    retransmits: Optional[int] = None

    # UDP only: measured at the receiver.
    jitter_ms: Optional[float] = None
    lost_packets: Optional[int] = None
    total_packets: Optional[int] = None
    lost_pct: Optional[float] = None

    # iperf3's error message if the test failed, e.g.
    # "unable to connect to server ... Connection refused".
    error_detail: Optional[str] = None


# ---------------------------------------------------------------------------
# PUBLIC FUNCTIONS
# ---------------------------------------------------------------------------

def load_server_config(path: Path = CONFIG_PATH) -> Tuple[str, int]:
    """Read the iperf3 server address and port from config.json."""

    with open(path) as f:
        config = json.load(f)

    iperf_config = config["iperf3"]
    return iperf_config["server"], int(iperf_config.get("port", DEFAULT_PORT))


def run_tcp(
    server: str,
    port: int = DEFAULT_PORT,
    duration_s: int = DEFAULT_DURATION_S,
    omit_s: int = DEFAULT_OMIT_S,
    download: bool = False,
) -> IperfResult:
    """
    Run one TCP throughput test.
    download=False: Pi sends (upload). download=True: server sends (-R).
    """

    test = TEST_TCP_DOWNLOAD if download else TEST_TCP_UPLOAD
    args = ["-t", str(duration_s), "-O", str(omit_s)]
    if download:
        args.append("-R")

    result = _new_result(test, server, port, duration_s)
    data, error = _run_iperf(server, port, args, duration_s + omit_s)

    if error:
        result.error_detail = error
        return result

    end = data["end"]
    result.success = True
    result.throughput_mbps = _mbps(end["sum_received"]["bits_per_second"])
    result.retransmits = end["sum_sent"].get("retransmits")
    return result


def run_udp(
    server: str,
    port: int = DEFAULT_PORT,
    duration_s: int = DEFAULT_DURATION_S,
    rate: str = DEFAULT_UDP_RATE,
) -> IperfResult:
    """Run one UDP test at a fixed rate (e.g. "500K") for jitter and loss."""

    args = ["-u", "-b", rate, "-t", str(duration_s)]

    result = _new_result(TEST_UDP, server, port, duration_s)
    data, error = _run_iperf(server, port, args, duration_s)

    if error:
        result.error_detail = error
        return result

    # Jitter and loss are measured by the receiver (the server).
    received = data["end"]["sum_received"]
    result.success = True
    result.throughput_mbps = _mbps(received["bits_per_second"])
    result.jitter_ms = round(received["jitter_ms"], 3)
    result.lost_packets = received["lost_packets"]
    result.total_packets = received["packets"]
    result.lost_pct = round(received["lost_percent"], 2)
    return result


# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------

def _run_iperf(
    server: str,
    port: int,
    args: List[str],
    test_time_s: int,
) -> Tuple[Optional[dict], Optional[str]]:
    """
    Run iperf3 as a client with JSON output.
    Returns (data, None) on success or (None, error message) on failure.
    """

    command = ["iperf3", "-c", server, "-p", str(port), "-J"] + args

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=test_time_s + RUN_TIMEOUT_MARGIN_S,
        )
    except subprocess.TimeoutExpired:
        return None, "iperf3 did not finish in time (server busy or stuck?)"
    except OSError as exc:
        # e.g. iperf3 is not installed
        return None, str(exc)

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        message = (proc.stderr or proc.stdout).strip()
        return None, message or f"iperf3 exited with code {proc.returncode}"

    # With -J, iperf3 reports failures (e.g. connection refused,
    # server busy) as an "error" entry in the JSON.
    if "error" in data:
        return None, data["error"]

    return data, None


def _new_result(test: str, server: str, port: int, duration_s: int) -> IperfResult:
    """Create a result with the common fields; timestamp = test start."""

    return IperfResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        test=test,
        server=server,
        port=port,
        duration_s=duration_s,
        success=False,
    )


def _mbps(bits_per_second: float) -> float:
    """Convert bits per second to Mbit/s, rounded to 2 decimals."""
    return round(bits_per_second / 1_000_000, 2)


if __name__ == "__main__":
    # Manual test: run all three tests one after another.
    parser = argparse.ArgumentParser(description="Run iperf3 tests.")
    parser.add_argument(
        "--server",
        help="server IP (default: the one in config.json)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION_S,
        help=f"seconds per test (default: {DEFAULT_DURATION_S})",
    )
    cli = parser.parse_args()

    server, port = load_server_config()
    if cli.server:
        server = cli.server

    print(run_tcp(server, port, cli.duration))
    time.sleep(PAUSE_BETWEEN_TESTS_S)
    print(run_tcp(server, port, cli.duration, download=True))
    time.sleep(PAUSE_BETWEEN_TESTS_S)
    print(run_udp(server, port, cli.duration))