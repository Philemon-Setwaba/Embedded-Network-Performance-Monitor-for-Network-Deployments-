"""
src/measurement/ping.py

Active measurement: round-trip latency and reachability via ICMP ping.

Implements:
  FR-1 - measure round-trip latency and reachability of the backhaul link.
  SP-1 - latency is measured at a configurable interval. The monitor uses
         10 s (set in config.json "ping.interval_s" and used by the
         scheduler); DEFAULT_INTERVAL_S below is only used when this file
         runs measurements on its own (run_continuous).

The implementation uses the system `ping` command rather than sending
raw ICMP packets directly. This avoids additional Python dependencies
and keeps the measurement code simple.

Besides the average RTT, the min / max / mdev values from ping's summary
line are kept as well (raw values, exactly as ping prints them), because
they show latency VARIATION within one measurement (event B4), e.g.
    22.0, 22.5, 21.9, 22.4, 32.3 ms -> avg 24.2 but max 32.3, mdev 4.0
"""

import dataclasses
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Maximum time to wait for a single ping reply.
DEFAULT_TIMEOUT_S = 2.0

# Time between measurements when running continuously (run_continuous).
# 10 s, the same as the monitor's interval in config.json (SP-1).
DEFAULT_INTERVAL_S = 10.0

# Number of ICMP packets to send per measurement.
# Default is 1 — satisfies FR-1 (reachability + latency).
# Increase to 5 or 10 to get a meaningful packet loss percentage,
# e.g. 3 packets lost out of 10 = 30% loss rather than always 0% or 100%.
# Note: total measurement time increases with count (5 packets take about
# 4 s when replies come back), so keep it well below the 10 s interval.
DEFAULT_PING_COUNT = 1

# Additional time allowed for the ping process before Python stops it.
SUBPROCESS_TIMEOUT_MARGIN_S = 1.0

# Loss reported when the ping process itself fails entirely (e.g. network
# unreachable, host unknown). This is different from partial packet loss.
LOSS_PCT_ON_PROCESS_FAILURE = 100.0

# Reasons a measurement can fail, stored in PingResult.error.
# iputils ping exits with 1 when no reply is received and 2 for other
# errors (e.g. unknown host, network unreachable), see `man ping`.
ERROR_NO_REPLY = "no_reply"        # ping exit code 1: sent, but no reply
ERROR_PING_ERROR = "ping_error"    # ping exit code 2 (or other non-zero)
ERROR_TIMEOUT = "timeout"          # ping did not finish; Python stopped it
ERROR_NO_RTT = "no_rtt"            # exit code 0, but no RTT in the output
ERROR_EXEC_FAILED = "exec_failed"  # ping could not be started at all

PING_EXIT_NO_REPLY = 1


# ---------------------------------------------------------------------------
# PING OUTPUT PARSING
# ---------------------------------------------------------------------------

# With a single packet, ping outputs a per-packet line like:
#   "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=23.4 ms"
# RTT_SINGLE_PATTERN extracts the RTT from that line.
RTT_SINGLE_PATTERN = re.compile(r"time=([\d.]+)\s*ms")

# ping adds a summary line at the end (when at least one reply came back):
#   "rtt min/avg/max/mdev = 21.935/24.223/32.278/4.033 ms"
# RTT_SUMMARY_PATTERN extracts ALL FOUR values: min, avg, max, mdev.
# (The average is used for rtt_ms when count > 1, as before.)
RTT_SUMMARY_PATTERN = re.compile(
    r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms"
)

# Extracts the packet loss percentage from ping's statistics line, e.g.:
#   "5 packets transmitted, 4 received, 20% packet loss, time 4003ms"
# This works correctly for any count — 0%, 20%, 100%, etc.
LOSS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)% packet loss")


@dataclasses.dataclass
class PingResult:
    """Stores the result of one ping measurement."""

    timestamp: str
    target: str
    reachable: bool   # True if at least one packet received a reply
    rtt_ms: Optional[float]   # Average RTT across all packets that replied
    loss_pct: float   # Percentage of packets that got no reply (0–100)

    # Why the measurement failed. None when the target replied.
    # One of the ERROR_* values defined above.
    error: Optional[str] = None

    # Extra detail for diagnosis, e.g. ping's own error message such as
    # "ping: connect: Network is unreachable". None when not available.
    error_detail: Optional[str] = None

    # From ping's summary line "rtt min/avg/max/mdev = ...", exactly as
    # printed. None when there was no reply (ping prints no summary then).
    rtt_min_ms: Optional[float] = None    # fastest reply
    rtt_max_ms: Optional[float] = None    # slowest reply
    rtt_mdev_ms: Optional[float] = None   # how much the replies vary


class PingMeasurer:
    """
    Measures round-trip latency and reachability of a target using the
    system `ping` command.

    The system uses the built-in Linux `ping` command instead of handling
    ICMP packets directly. This keeps the implementation simple and avoids
    extra dependencies, making the system easier to deploy and maintain
    (NFR-5).
    """

    def __init__(
        self,
        target: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        count: int = DEFAULT_PING_COUNT,
    ):
        # iputils ping treats `-W 0` as an infinite wait (see `man ping`),
        # so a timeout of 0 or less is rejected at start-up rather than
        # silently causing every measurement to hang until the Python
        # safety timeout stops it.
        if timeout_s <= 0:
            raise ValueError(
                "timeout_s must be greater than 0 "
                "(ping treats -W 0 as an infinite wait)"
            )

        # count must be at least 1 — sending zero packets makes no sense.
        if count < 1:
            raise ValueError("count must be at least 1")

        self.target = target
        self.timeout_s = timeout_s
        self.count = count  # number of ICMP packets per measurement

    # -----------------------------------------------------------------------
    # Single measurement
    # -----------------------------------------------------------------------

    def measure_once(self) -> PingResult:
        """Send `self.count` ICMP echo requests and return the result."""

        timestamp = self._now_utc_iso()

        stdout, error, error_detail = self._run_ping_subprocess()

        if error is not None:
            # The ping process itself failed — not just packet loss.
            return self._failed_result(timestamp, error, error_detail)

        # Extract average RTT and loss from ping's output.
        rtt_ms = self._extract_rtt_ms(stdout, self.count)
        loss_pct = self._extract_loss_pct(stdout)

        if rtt_ms is None:
            return self._failed_result(timestamp, ERROR_NO_RTT, None)

        # min / max / mdev from the summary line (None if it is missing).
        summary = self._extract_rtt_summary(stdout)
        rtt_min_ms, rtt_max_ms, rtt_mdev_ms = (
            (summary[0], summary[2], summary[3]) if summary else (None, None, None)
        )

        # reachable = True if at least one packet got a reply (loss < 100%).
        reachable = loss_pct < 100.0

        return PingResult(
            timestamp=timestamp,
            target=self.target,
            reachable=reachable,
            rtt_ms=rtt_ms,
            loss_pct=loss_pct,
            rtt_min_ms=rtt_min_ms,
            rtt_max_ms=rtt_max_ms,
            rtt_mdev_ms=rtt_mdev_ms,
        )

    # -----------------------------------------------------------------------
    # Continuous measurement
    # -----------------------------------------------------------------------

    def run_continuous(
        self,
        interval_s: float = DEFAULT_INTERVAL_S,
        on_result=None,
        stop_after: Optional[int] = None,
    ):
        """
        Perform measurements repeatedly at the specified interval.

        on_result:
            Optional callback that receives each PingResult. This can later
            be connected to the reporting buffer and dashboard.

        stop_after:
            Number of measurements to perform. If None, the measurements
            continue indefinitely.
        """

        count = 0

        while stop_after is None or count < stop_after:
            loop_start = time.monotonic()

            result = self.measure_once()
            self._deliver_result(result, on_result)

            count += 1

            self._sleep_until_next_interval(loop_start, interval_s)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _now_utc_iso() -> str:
        """Return the current UTC time in ISO 8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def _run_ping_subprocess(
        self,
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Run the system ping command.

        Returns:
            (stdout, error, error_detail)
            - On success: (ping stdout, None, None)
            - On failure: (None, one of the ERROR_* values, detail or None)
        """

        ping_wait_s = f"{self.timeout_s:g}"

        # The Python safety timeout must cover ALL packets, not just one.
        # Each packet can wait up to timeout_s before giving up, so the
        # worst case is count * timeout_s total wait time.
        subprocess_timeout_s = (
            self.count * self.timeout_s + SUBPROCESS_TIMEOUT_MARGIN_S
        )

        try:
            proc = subprocess.run(
                [
                    "ping",
                    "-c", str(self.count),   # number of packets
                    "-W", ping_wait_s,        # per-packet reply timeout
                    self.target,
                ],
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None, ERROR_TIMEOUT, None
        except OSError as exc:
            return None, ERROR_EXEC_FAILED, str(exc)

        if proc.returncode == 0:
            return proc.stdout, None, None

        # returncode 1 means packets were sent but none replied.
        # We still return stdout here so we can extract loss_pct from
        # ping's statistics line (e.g. "100% packet loss").
        if proc.returncode == PING_EXIT_NO_REPLY:
            return proc.stdout, ERROR_NO_REPLY, self._first_line(proc.stderr)

        return None, ERROR_PING_ERROR, self._first_line(proc.stderr)

    @staticmethod
    def _extract_rtt_ms(ping_stdout: str, count: int) -> Optional[float]:
        """
        Extract the RTT value from ping output.

        With count=1, uses the per-packet 'time=X ms' line.
        With count>1, uses the average from the summary line:
          'rtt min/avg/max/mdev = X/AVG/X/X ms'
        The average is more representative than any single packet.
        """

        if count > 1:
            # Try the summary line first (more accurate for multiple packets).
            match = RTT_SUMMARY_PATTERN.search(ping_stdout)
            if match:
                return float(match.group(2))   # group 2 = avg

        # Fall back to per-packet line (also works for count=1).
        match = RTT_SINGLE_PATTERN.search(ping_stdout)
        if match:
            return float(match.group(1))

        return None

    @staticmethod
    def _extract_rtt_summary(
        ping_stdout: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        """
        Extract (min, avg, max, mdev) from ping's summary line, e.g.
          'rtt min/avg/max/mdev = 21.935/24.223/32.278/4.033 ms'
          -> (21.935, 24.223, 32.278, 4.033)
        Returns None if the line is missing (e.g. no reply at all).
        """

        match = RTT_SUMMARY_PATTERN.search(ping_stdout)
        if not match:
            return None
        return tuple(float(value) for value in match.groups())

    @staticmethod
    def _extract_loss_pct(ping_stdout: str) -> float:
        """
        Extract packet loss percentage from ping's statistics output.

        Returns 0.0 if the loss line is not found (assumes success).
        ping always prints this line even for 100% loss, so 'not found'
        means something unexpected happened with ping's output format.
        """

        match = LOSS_PATTERN.search(ping_stdout)
        if match:
            return float(match.group(1))

        # Could not find the loss line — assume 0 (no loss).
        return 0.0

    @staticmethod
    def _first_line(text: Optional[str]) -> Optional[str]:
        """Return the first non-empty line of text, or None."""

        if not text:
            return None

        for line in text.splitlines():
            if line.strip():
                return line.strip()

        return None

    def _failed_result(
        self,
        timestamp: str,
        error: str,
        error_detail: Optional[str],
    ) -> PingResult:
        """Create a result representing a completely failed measurement."""

        return PingResult(
            timestamp=timestamp,
            target=self.target,
            reachable=False,
            rtt_ms=None,
            loss_pct=LOSS_PCT_ON_PROCESS_FAILURE,
            error=error,
            error_detail=error_detail,
        )

    @staticmethod
    def _deliver_result(result: PingResult, on_result) -> None:
        """Send the result to the callback or print it."""

        if on_result:
            on_result(result)
        else:
            print(result)

    @staticmethod
    def _sleep_until_next_interval(
        loop_start: float,
        interval_s: float,
    ) -> None:
        """Wait until the next measurement interval."""

        elapsed = time.monotonic() - loop_start
        remaining = max(0.0, interval_s - elapsed)

        time.sleep(remaining)


if __name__ == "__main__":
    import sys

    # Read packet count from command line, default to 1.
    # Usage examples:
    #   python3 ping.py           → 1 packet (default)
    #   python3 ping.py 5         → 5 packets
    #   python3 ping.py 10        → 10 packets
    count = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PING_COUNT

    print(f"Testing with count={count} packets per measurement, 3 measurements:\n")

    measurer = PingMeasurer(target="8.8.8.8", count=count)
    measurer.run_continuous(interval_s=5.0, stop_after=3)