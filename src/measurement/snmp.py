"""
src/measurement/passive.py

Passive measurement: interface traffic counters via SNMP.

Implements:
  FR-4 - passively poll interface/traffic counters where an SNMP agent
         is available.
  SP-4 - SNMP interface counters are polled at a configurable interval
         using SNMP GET requests.

This is "passive" because the monitor reads traffic counters that the
network device already maintains instead of generating test traffic.

Uses the system `snmpget` command (net-snmp) rather than a Python SNMP
library. This avoids an additional Python dependency and uses the
standard `snmp` package available on Raspberry Pi OS.
"""

import dataclasses
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional, Tuple


# CONFIG

# Default SNMP community string for testing.
# Replace this with the configured community string for the target device.
DEFAULT_COMMUNITY = "public"

DEFAULT_SNMP_VERSION = "2c"

# Time between measurements when running continuously.
# The default polling interval is 30 seconds.
DEFAULT_INTERVAL_S = 30.0

# Maximum time to wait for an SNMP response.
DEFAULT_TIMEOUT_S = 3.0


# IF-MIB INTERFACE COUNTER OIDs

IF_IN_OCTETS_OID_BASE = "1.3.6.1.2.1.2.2.1.10"
IF_OUT_OCTETS_OID_BASE = "1.3.6.1.2.1.2.2.1.16"


@dataclasses.dataclass
class SNMPPollResult:
    """Stores the result of one SNMP poll."""

    timestamp: str
    target: str
    if_index: int
    reachable: bool
    in_octets: Optional[int]
    out_octets: Optional[int]
    in_bps: Optional[float]
    out_bps: Optional[float]


class SNMPPoller:
    """
    Polls ifInOctets and ifOutOctets for one interface on one SNMP agent.

    The difference between consecutive counter readings is used to
    calculate the traffic rate in bits per second.

    One poller instance represents one target and one interface.
    """

    def __init__(
        self,
        target: str,
        if_index: int,
        community: str = DEFAULT_COMMUNITY,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.target = target
        self.if_index = if_index
        self.community = community
        self.timeout_s = timeout_s

        # Store the previous successful counter values so that the
        # traffic rate can be calculated during the next successful poll.
        self._last_octets: Optional[Tuple[int, int]] = None

        # Store when the previous successful poll was completed.
        self._last_poll_monotonic: Optional[float] = None

    def poll_once(self) -> SNMPPollResult:
        """Perform one SNMP poll and return the measurement result."""

        timestamp = self._now_utc_iso()
        poll_monotonic = time.monotonic()

        stdout = self._run_snmpget_subprocess()
        counters = self._parse_counters(stdout) if stdout else None

        # SNMP request failed or the response could not be parsed.
        if counters is None:
            return SNMPPollResult(
                timestamp=timestamp,
                target=self.target,
                if_index=self.if_index,
                reachable=False,
                in_octets=None,
                out_octets=None,
                in_bps=None,
                out_bps=None,
            )

        in_octets, out_octets = counters

        # Calculate the traffic rate using the previous successful poll.
        in_bps, out_bps = self._compute_bps(
            counters,
            poll_monotonic,
        )

        # Only update the previous values after a successful poll.
        self._last_octets = counters
        self._last_poll_monotonic = poll_monotonic

        return SNMPPollResult(
            timestamp=timestamp,
            target=self.target,
            if_index=self.if_index,
            reachable=True,
            in_octets=in_octets,
            out_octets=out_octets,
            in_bps=in_bps,
            out_bps=out_bps,
        )

    def run_continuous(
        self,
        interval_s: float = DEFAULT_INTERVAL_S,
        on_result=None,
        stop_after: Optional[int] = None,
    ):
        """
        Continuously perform SNMP polls at the configured interval.

        stop_after can be used during testing to stop after a fixed
        number of measurements.
        """

        count = 0

        while stop_after is None or count < stop_after:
            loop_start = time.monotonic()

            result = self.poll_once()

            if on_result:
                on_result(result)
            else:
                print(result)

            count += 1

            # Sleep only for the remaining part of the interval.
            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, interval_s - elapsed))

    @staticmethod
    def _now_utc_iso() -> str:
        """Return the current UTC time as an ISO 8601 string."""

        return datetime.now(timezone.utc).isoformat()

    def _run_snmpget_subprocess(self) -> Optional[str]:
        """
        Run one snmpget request for both interface counters.

        Both OIDs are requested in one SNMP request.
        """

        in_oid = f"{IF_IN_OCTETS_OID_BASE}.{self.if_index}"
        out_oid = f"{IF_OUT_OCTETS_OID_BASE}.{self.if_index}"

        try:
            proc = subprocess.run(
                [
                    "snmpget",
                    "-v", DEFAULT_SNMP_VERSION,
                    "-c", self.community,
                    "-O", "qv",
                    "-t", str(self.timeout_s),  # wait this long for an SNMP response
                    "-r", "0",
                    self.target,
                    in_oid,
                    out_oid,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 1.0,
            )

        except subprocess.TimeoutExpired:
            return None

        if proc.returncode != 0:
            return None

        return proc.stdout

    @staticmethod
    def _parse_counters(
        stdout: str,
    ) -> Optional[Tuple[int, int]]:
        """
        Parse the input and output octet counters from snmpget output.

        With `-O qv`, snmpget returns only the values, one per line -
        exactly one line per OID requested. We ask for exactly two OIDs
        (in_octets, out_octets), so we require exactly two non-blank
        lines back. Anything else (more lines, fewer lines) means the
        output isn't what we expected, and we fail loudly (return None,
        which becomes an "unreachable" result) rather than silently
        guessing which lines are the values we want.
        """

        lines = [
            line.strip()
            for line in stdout.splitlines()
            if line.strip()
        ]

        if len(lines) != 2:
            return None

        try:
            in_octets = int(lines[0])
            out_octets = int(lines[1])
        except ValueError:
            return None

        return in_octets, out_octets

    def _compute_bps(
        self,
        counters: Tuple[int, int],
        current_time: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate incoming and outgoing traffic rates in bits per second.

        The first successful poll cannot produce a rate because there is
        no previous counter value yet.
        """

        if self._last_octets is None or self._last_poll_monotonic is None:
            return None, None

        elapsed_s = current_time - self._last_poll_monotonic

        if elapsed_s <= 0:
            return None, None

        previous_in, previous_out = self._last_octets
        current_in, current_out = counters

        delta_in = current_in - previous_in
        delta_out = current_out - previous_out

        # A negative counter difference can happen if the counter wraps
        # around or the network device resets its counters.
        # In this case, the rate cannot be trusted.
        if delta_in < 0 or delta_out < 0:
            return None, None

        in_bps = (delta_in * 8) / elapsed_s
        out_bps = (delta_out * 8) / elapsed_s

        return in_bps, out_bps


if __name__ == "__main__":
    # Manual test: perform five measurements at five-second intervals.
    # The deployment interval is 30 seconds by default.
    poller = SNMPPoller(
        target="127.0.0.1",
        if_index=2,
    )

    poller.run_continuous(
        interval_s=5.0,
        stop_after=5,
    )