"""
src/measurement/active.py

Active measurement: round-trip latency and reachability via ICMP ping.

Implements FR-1 (measure round-trip latency and reachability of the
backhaul link) and SP-1 (measured at least every 30 seconds).
"""

import re
import subprocess
import time
import dataclasses
from datetime import datetime, timezone
from typing import Optional


@dataclasses.dataclass
class PingResult:
    timestamp: str          # ISO 8601 UTC
    target: str
    reachable: bool
    rtt_ms: Optional[float]  # None if unreachable
    raw_loss_pct: float      # 0.0 or 100.0 for a single probe


class PingMeasurer:
    """
    Wraps the system `ping` binary to measure round-trip latency and
    reachability of a single target (the backhaul link's next hop or a
    known-stable endpoint beyond it).

    Uses the system binary rather than a raw-socket library (e.g. scapy)
    so it runs without extra permissions or dependencies on stock
    Raspberry Pi OS (iputils-ping is installed by default).
    """

    # Matches: "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=23.4 ms"
    _RTT_PATTERN = re.compile(r"time=([\d.]+)\s*ms")

    def __init__(self, target: str, timeout_s: float = 2.0):
        self.target = target
        self.timeout_s = timeout_s

    def measure_once(self) -> PingResult:
        """Send a single ICMP echo request and parse the result."""
        timestamp = datetime.now(timezone.utc).isoformat()

        try:
            proc = subprocess.run(
                ["ping", "-c", "1", "-W", str(int(self.timeout_s)), self.target],
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 1.0,
            )
        except subprocess.TimeoutExpired:
            return PingResult(timestamp, self.target, False, None, 100.0)

        if proc.returncode != 0:
            # Host unreachable, network unreachable, or no reply
            return PingResult(timestamp, self.target, False, None, 100.0)

        match = self._RTT_PATTERN.search(proc.stdout)
        if not match:
            # Reachable per exit code but couldn't parse RTT — treat as
            # a failed measurement rather than silently reporting 0ms
            return PingResult(timestamp, self.target, False, None, 100.0)

        rtt_ms = float(match.group(1))
        return PingResult(timestamp, self.target, True, rtt_ms, 0.0)

    def run_continuous(self, interval_s: float = 30.0, on_result=None, stop_after: Optional[int] = None):
        """
        Measure repeatedly at a fixed interval (default 30s per SP-1).

        on_result: optional callback(PingResult) -> None, e.g. to hand
        results off to the local buffer (src/reporting/buffer.py) and
        the dashboard (src/dashboard/).
        stop_after: if set, stop after this many measurements (useful
        for testing); if None, runs forever.
        """
        count = 0
        while stop_after is None or count < stop_after:
            start = time.monotonic()
            result = self.measure_once()

            if on_result:
                on_result(result)
            else:
                print(result)

            count += 1
            elapsed = time.monotonic() - start
            sleep_time = max(0.0, interval_s - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    # Quick manual test: ping a known-stable public resolver every 5s,
    # 5 times, so you can eyeball it working before wiring it to the
    # buffer/dashboard.
    measurer = PingMeasurer(target="8.8.8.8")
    measurer.run_continuous(interval_s=5.0, stop_after=5)
