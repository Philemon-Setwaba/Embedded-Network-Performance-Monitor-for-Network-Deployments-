"""
src/measurement/ping.py

Active measurement: round-trip latency and reachability via ICMP ping.

Implements:
  FR-1 - measure round-trip latency and reachability of the backhaul link.
  SP-1 - latency is measured at a configurable interval, with a default
         interval of 30 seconds.

The implementation uses the system `ping` command rather than sending
raw ICMP packets directly. This avoids additional Python dependencies
and keeps the measurement code simple.
"""

import dataclasses
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Maximum time to wait for a single ping reply.
DEFAULT_TIMEOUT_S = 2.0

# Time between measurements when running continuously.
# The default of 30 seconds satisfies SP-1.
DEFAULT_INTERVAL_S = 30.0

# Send one ICMP echo request per measurement.
PING_COUNT = 1

# Additional time allowed for the ping process before Python stops it.
SUBPROCESS_TIMEOUT_MARGIN_S = 1.0

# Loss reported for a failed or successful single probe.
LOSS_PCT_ON_FAILURE = 100.0
LOSS_PCT_ON_SUCCESS = 0.0


# ---------------------------------------------------------------------------
# PING OUTPUT PARSING
# ---------------------------------------------------------------------------

# Matches the RTT value from ping output, for example:
# "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=23.4 ms"
RTT_PATTERN = re.compile(r"time=([\d.]+)\s*ms")


@dataclasses.dataclass
class PingResult:
    """Stores the result of one ping measurement."""

    timestamp: str
    target: str
    reachable: bool
    rtt_ms: Optional[float]
    loss_pct: float


class PingMeasurer:
    """
    Measures round-trip latency and reachability of a target using the
    system `ping` command.

    The system uses the built-in Linux `ping` command instead of handling
    ICMP packets directly. This keeps the implementation simple and avoids
    extra dependencies, making the system easier to deploy and maintain
    (NFR-5).
    """

    def __init__(self, target: str, timeout_s: float = DEFAULT_TIMEOUT_S):
        # iputils ping treats `-W 0` as an infinite wait (see `man ping`),
        # so a timeout of 0 or less is rejected at start-up rather than
        # silently causing every measurement to hang until the Python
        # safety timeout stops it.
        if timeout_s <= 0:
            raise ValueError(
                "timeout_s must be greater than 0 "
                "(ping treats -W 0 as an infinite wait)"
            )

        self.target = target
        self.timeout_s = timeout_s

    # -----------------------------------------------------------------------
    # Single measurement
    # -----------------------------------------------------------------------

    def measure_once(self) -> PingResult:
        """Send one ICMP echo request and return the measurement result."""

        timestamp = self._now_utc_iso()

        stdout = self._run_ping_subprocess()

        if stdout is None:
            return self._failed_result(timestamp)

        rtt_ms = self._extract_rtt_ms(stdout)

        if rtt_ms is None:
            return self._failed_result(timestamp)

        return PingResult(
            timestamp=timestamp,
            target=self.target,
            reachable=True,
            rtt_ms=rtt_ms,
            loss_pct=LOSS_PCT_ON_SUCCESS,
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

    def _run_ping_subprocess(self) -> Optional[str]:
        """
        Run the system ping command.

        Returns:
            ping stdout if the command succeeds, otherwise None.
        """

        # `-W` tells ping how many seconds to wait for a reply before
        # considering the ping unsuccessful. iputils ping accepts decimal
        # values (e.g. 0.5), and 0 means wait forever (see `man ping`),
        # so the timeout is passed as-is rather than rounded to an int.
        # The Python timeout provides a second safety limit and stops the
        # ping process if it takes too long to finish.

        ping_wait_s = f"{self.timeout_s:g}"
        subprocess_timeout_s = self.timeout_s + SUBPROCESS_TIMEOUT_MARGIN_S

        try:
            proc = subprocess.run(
                [
                    "ping",
                    "-c",
                    str(PING_COUNT),
                    "-W",
                    ping_wait_s,
                    self.target,
                ],
                capture_output=True,
                text=True,
                timeout=subprocess_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return None

        if proc.returncode != 0:
            return None

        return proc.stdout

    @staticmethod
    def _extract_rtt_ms(ping_stdout: str) -> Optional[float]:
        """Extract the RTT value in milliseconds from ping output."""

        match = RTT_PATTERN.search(ping_stdout)

        if not match:
            return None

        return float(match.group(1))

    def _failed_result(self, timestamp: str) -> PingResult:
        """Create a result representing a failed ping measurement."""

        return PingResult(
            timestamp=timestamp,
            target=self.target,
            reachable=False,
            rtt_ms=None,
            loss_pct=LOSS_PCT_ON_FAILURE,
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
    # Manual test: perform five measurements at five-second intervals.
    # This is only for testing; the deployment interval is 30 seconds.
    measurer = PingMeasurer(target="8.8.8.8")
    measurer.run_continuous(interval_s=5.0, stop_after=5)