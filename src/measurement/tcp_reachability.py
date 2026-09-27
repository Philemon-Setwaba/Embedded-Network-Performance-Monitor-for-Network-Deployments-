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

A connection that is accepted, or refused by the target, shows that
a TCP path to the endpoint is available (a refusal is a reply from
the target itself). A timeout or network error, together with failed
ping and iperf3 checks, provides stronger evidence that the backhaul
is unavailable and that measurements should be buffered locally.
"""

import dataclasses
import errno
import socket
import time
from datetime import datetime, timezone
from typing import Optional


# Default endpoint and timeout for the TCP connectivity check
DEFAULT_TARGET = "1.1.1.1"  # Cloudflare DNS endpoint
DEFAULT_PORT = 443
DEFAULT_TIMEOUT_S = 3.0

# Possible outcomes, stored in TCPReachabilityResult.status
STATUS_CONNECTED = "connected"                  # target accepted the connection
STATUS_REFUSED = "refused"                      # target replied "port closed": path is up
STATUS_TIMEOUT = "timeout"                      # no reply: path down or filtered
STATUS_NETWORK_UNREACHABLE = "network_unreachable"  # Pi's own network is down
STATUS_OTHER_ERROR = "other_error"              # any other failure (see error_detail)


@dataclasses.dataclass
class TCPReachabilityResult:
    """Stores the result of a TCP connectivity check."""
    timestamp: str
    target: str
    port: int

    # True if the target replied (connected or refused), i.e. a TCP path
    # through the backhaul exists.
    reachable: bool

    # Time until the target replied, in ms. None if it did not reply.
    connect_time_ms: Optional[float]

    # One of the STATUS_* values above.
    status: str

    # Error message for diagnosis, e.g. "[Errno 101] Network is unreachable".
    # None when the connection succeeded.
    error_detail: Optional[str] = None


def check_reachability(
    target: str = DEFAULT_TARGET,
    port: int = DEFAULT_PORT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> TCPReachabilityResult:
    """Try one TCP connection to the target and return the result."""

    # Record the measurement time and start the connection timer
    timestamp = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()

    def elapsed_ms() -> float:
        return round((time.monotonic() - start) * 1000, 1)

    def result(reachable, connect_time_ms, status, error_detail=None):
        return TCPReachabilityResult(
            timestamp=timestamp,
            target=target,
            port=port,
            reachable=reachable,
            connect_time_ms=connect_time_ms,
            status=status,
            error_detail=error_detail,
        )

    try:
        # Attempt to establish a TCP connection to the target
        with socket.create_connection((target, port), timeout=timeout_s):
            return result(True, elapsed_ms(), STATUS_CONNECTED)

    except TimeoutError as exc:
        # No reply within the timeout (socket.timeout is TimeoutError)
        return result(False, None, STATUS_TIMEOUT, str(exc) or None)

    except ConnectionRefusedError as exc:
        # The target itself replied that the port is closed,
        # so the path through the backhaul is working.
        return result(True, elapsed_ms(), STATUS_REFUSED, str(exc))

    except OSError as exc:
        if exc.errno == errno.ENETUNREACH:
            return result(False, None, STATUS_NETWORK_UNREACHABLE, str(exc))

        return result(False, None, STATUS_OTHER_ERROR, str(exc))


if __name__ == "__main__":
    # Run the check when this file is executed directly
    print(check_reachability())