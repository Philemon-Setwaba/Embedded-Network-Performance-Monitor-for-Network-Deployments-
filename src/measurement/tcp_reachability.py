"""
src/measurement/tcp_reachability.py

Final backhaul connectivity check using a plain TCP connection to
a known endpoint on port 443 (HTTPS).

This check is used after the normal ping and iperf3 measurements
fail. Its purpose is to determine whether the device can still
establish a general outbound TCP connection through the backhaul.

A TCP connection is used as a final fallback because ICMP may be
blocked or filtered, while iperf3 requires a dedicated measurement
server at the remote end. This check requires neither ICMP nor a
dedicated iperf3 server.

A successful connection indicates that a TCP path to the selected
endpoint is available. A failed connection, together with failed
ping and iperf3 checks, provides stronger evidence that the backhaul
is unavailable and that measurements should be buffered locally.
"""

import socket
import time
import dataclasses
from datetime import datetime, timezone
from typing import Optional


# Default endpoint and timeout for the TCP connectivity check
DEFAULT_TARGET = "1.1.1.1"  # Cloudflare DNS endpoint
DEFAULT_PORT = 443
DEFAULT_TIMEOUT_S = 3.0


@dataclasses.dataclass
class TCPReachabilityResult:
    """Stores the result of a TCP connectivity check."""
    timestamp: str
    target: str
    port: int
    reachable: bool
    connect_time_ms: Optional[float]


def check_reachability(
    target: str = DEFAULT_TARGET,
    port: int = DEFAULT_PORT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> TCPReachabilityResult:

    # Record the measurement time and start the connection timer
    timestamp = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()

    try:
        # Attempt to establish a TCP connection to the target
        with socket.create_connection((target, port), timeout=timeout_s):
            connect_time_ms = (time.monotonic() - start) * 1000

            return TCPReachabilityResult(
                timestamp,
                target,
                port,
                True,
                connect_time_ms,
            )

    except (socket.timeout, OSError):
        # Connection failed or timed out
        return TCPReachabilityResult(
            timestamp,
            target,
            port,
            False,
            None,
        )


if __name__ == "__main__":
    # Run the check when this file is executed directly
    result = check_reachability()
    print(result)
