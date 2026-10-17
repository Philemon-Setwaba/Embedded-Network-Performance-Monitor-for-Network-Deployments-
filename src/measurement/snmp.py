"""
src/measurement/snmp.py

Passive measurement: interface traffic counters via SNMP.

Implements:
  FR-4 - passively poll interface/traffic counters where an SNMP agent
         is available.
  SP-4 - SNMP interface counters are polled at a configurable interval
         using SNMP GET requests.

This is "passive" because the monitor reads traffic counters that the
network device already maintains instead of generating test traffic.

Uses the system `snmpget` / `snmpwalk` commands (net-snmp) rather than a
Python SNMP library. This avoids an additional Python dependency and uses
the standard `snmp` package available on Raspberry Pi OS.

The target, interface and community string are read from config.json in
the repository root, so they can be changed (e.g. from the Pi itself to a
community network router) without editing code.

Interface by NAME (recommended) or by NUMBER:
  "interface": "lte0"  - the interface is looked up by name (ifDescr) on
                         EVERY poll. Needed because interface numbers are
                         not stable on every device: on the Mercusys
                         MB135-4G router, lte0 moved from index 8 to 18
                         while the router stayed up, and it disappears
                         while the LTE link is down.
  "if_index": 3        - fixed interface number (old behaviour). Only used
                         when "interface" is not set.
"""

import argparse
import dataclasses
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple


# CONFIG

# config.json in the repository root (src/measurement/snmp.py -> ../../)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"

# Defaults, used when config.json has no "snmp" section.
DEFAULT_TARGET = "127.0.0.1"

# Default SNMP community string for testing.
# Replace this with the configured community string for the target device.
DEFAULT_COMMUNITY = "public"

# Interface to poll when none is given. Interface numbers differ per
# device; list them with:
#   snmpwalk -v2c -c public <target> 1.3.6.1.2.1.2.2.1.2
# On the test Pi: 1 = lo, 2 = eth0, 3 = wlan0.
DEFAULT_IF_INDEX = 3

DEFAULT_SNMP_VERSION = "2c"

# Time between measurements when running continuously.
# The default polling interval is 30 seconds.
DEFAULT_INTERVAL_S = 30.0

# Maximum time to wait for an SNMP response.
DEFAULT_TIMEOUT_S = 3.0


# IF-MIB OIDs

# Interface names (ifDescr), used to find the interface number by name.
IF_DESCR_OID_BASE = "1.3.6.1.2.1.2.2.1.2"

# 64-bit counters (ifHCInOctets / ifHCOutOctets, ifXTable). Preferred:
# in practice they never wrap around.
IF_HC_IN_OCTETS_OID_BASE = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS_OID_BASE = "1.3.6.1.2.1.31.1.1.1.10"

# 32-bit counters (ifInOctets / ifOutOctets). Fallback for agents that do
# not provide 64-bit counters (e.g. the Mercusys router). They wrap around
# after 2^32 bytes (about 4.3 GB), e.g. roughly every 6 minutes at
# 100 Mbit/s.
IF_IN_OCTETS_OID_BASE = "1.3.6.1.2.1.2.2.1.10"
IF_OUT_OCTETS_OID_BASE = "1.3.6.1.2.1.2.2.1.16"

COUNTER_OIDS = {
    64: (IF_HC_IN_OCTETS_OID_BASE, IF_HC_OUT_OCTETS_OID_BASE),
    32: (IF_IN_OCTETS_OID_BASE, IF_OUT_OCTETS_OID_BASE),
}


@dataclasses.dataclass
class SNMPPollResult:
    """Stores the result of one SNMP poll."""

    timestamp: str
    target: str
    if_index: int

    # True if the agent replied, even if the reply held no usable
    # counters (e.g. wrong interface number; see error_detail).
    reachable: bool

    in_octets: Optional[int]
    out_octets: Optional[int]
    in_bps: Optional[float]
    out_bps: Optional[float]

    # Which counters were read: 64 (ifHC*) or 32 (ifIn/OutOctets).
    # None if no counters could be read.
    counter_bits: Optional[int] = None

    # Why no counters were read, e.g. "Timeout: No Response from ..." or
    # "No Such Instance currently exists at this OID (check if_index)".
    # None when the poll worked.
    error_detail: Optional[str] = None

    # Interface name from config.json ("interface"), e.g. "lte0" or
    # "wlan0". None when a fixed if_index is used instead.
    if_name: Optional[str] = None


def load_snmp_config(path: Path = CONFIG_PATH) -> Tuple[str, int, str]:
    """
    Read target, interface number and community from the "snmp" section
    of config.json. Missing file, section or keys fall back to defaults.
    """

    snmp_config = _read_snmp_section(path)

    return (
        snmp_config.get("target", DEFAULT_TARGET),
        int(snmp_config.get("if_index", DEFAULT_IF_INDEX)),
        snmp_config.get("community", DEFAULT_COMMUNITY),
    )


def load_snmp_interface_name(path: Path = CONFIG_PATH) -> Optional[str]:
    """
    Read the interface NAME ("interface") from the "snmp" section of
    config.json, e.g. "lte0". None if not set (then if_index is used).
    """

    name = _read_snmp_section(path).get("interface")
    return str(name) if name else None


def _read_snmp_section(path: Path) -> dict:
    """The "snmp" section of config.json, or {} if missing."""

    try:
        with open(path) as f:
            return json.load(f).get("snmp", {})
    except FileNotFoundError:
        return {}


class SNMPPoller:
    """
    Polls the in/out octet counters for one interface on one SNMP agent.

    If if_name is given, the interface number is looked up by name on
    every poll (see module docstring). Otherwise the fixed if_index is
    used.

    64-bit counters (ifHCInOctets/ifHCOutOctets) are tried first. If the
    agent replies but does not provide them, the 32-bit counters
    (ifInOctets/ifOutOctets) are used instead. The choice is remembered,
    so later polls need only one request.

    The difference between consecutive counter readings is used to
    calculate the traffic rate in bits per second.

    One poller instance represents one target and one interface.
    """

    def __init__(
        self,
        target: str,
        if_index: int = DEFAULT_IF_INDEX,
        community: str = DEFAULT_COMMUNITY,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        if_name: Optional[str] = None,
    ):
        self.target = target
        self.if_index = if_index
        self.community = community
        self.timeout_s = timeout_s
        self.if_name = if_name

        # Store the previous successful counter values so that the
        # traffic rate can be calculated during the next successful poll.
        self._last_octets: Optional[Tuple[int, int]] = None

        # Store when the previous successful poll was completed.
        self._last_poll_monotonic: Optional[float] = None

        # 64 or 32 once known; None until counters have been read once.
        self._counter_bits: Optional[int] = None

    def poll_once(self) -> SNMPPollResult:
        """Perform one SNMP poll and return the measurement result."""

        timestamp = self._now_utc_iso()
        poll_monotonic = time.monotonic()

        # 0. Interface by name: find its current number first.
        if self.if_name:
            found_index, replied, error_detail = self._find_if_index()

            if found_index is None:
                # Agent did not answer, or the interface does not exist
                # right now (e.g. lte0 while the LTE link is down).
                # Forget the old counters: when the interface comes back
                # its counters may have restarted.
                self._forget_counters()
                return self._failed_result(timestamp, replied, error_detail)

            if found_index != self.if_index:
                # The interface got a new number: its counters belong to
                # a different interface instance, so do not compare them
                # with the old ones.
                self.if_index = found_index
                self._forget_counters()

        counters, bits, replied, error_detail = self._read_counters()

        # SNMP request failed or the reply held no usable counters.
        if counters is None:
            return self._failed_result(timestamp, replied, error_detail)

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
            counter_bits=bits,
            if_name=self.if_name,
        )

    def _failed_result(
        self,
        timestamp: str,
        replied: bool,
        error_detail: Optional[str],
    ) -> SNMPPollResult:
        """A result without counters (see poll_once)."""

        return SNMPPollResult(
            timestamp=timestamp,
            target=self.target,
            if_index=self.if_index,
            reachable=replied,
            in_octets=None,
            out_octets=None,
            in_bps=None,
            out_bps=None,
            error_detail=error_detail,
            if_name=self.if_name,
        )

    def _forget_counters(self):
        """Drop the previous counters, so the next rate starts fresh."""

        self._last_octets = None
        self._last_poll_monotonic = None

    def _find_if_index(self) -> Tuple[Optional[int], bool, Optional[str]]:
        """
        Find the number of the interface called self.if_name.

        Returns (if_index, replied, error_detail):
          if_index     the interface number, or None if not found
          replied      True if the agent sent any reply
          error_detail why it was not found, or None
        """

        names, error_detail = self._walk_if_names()

        if names is None:
            return None, False, error_detail

        for index, name in names.items():
            if name == self.if_name:
                return index, True, None

        return None, True, f"interface '{self.if_name}' not found on {self.target}"

    def _walk_if_names(self) -> Tuple[Optional[Dict[int, str]], Optional[str]]:
        """
        Read all interface names (ifDescr) with one snmpwalk.

        Returns ({if_index: name}, None) if the agent replied, or
        (None, error message) if there was no reply or snmpwalk failed.
        """

        try:
            proc = subprocess.run(
                [
                    "snmpwalk",
                    "-v", DEFAULT_SNMP_VERSION,
                    "-c", self.community,
                    "-O", "nq",           # numeric OIDs, "OID value" per line
                    "-t", str(self.timeout_s),
                    "-r", "0",
                    self.target,
                    IF_DESCR_OID_BASE,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_s + 2.0,
            )

        except subprocess.TimeoutExpired:
            return None, f"snmpwalk did not finish within {self.timeout_s + 2.0:g} s"
        except OSError as exc:
            # e.g. snmpwalk is not installed
            return None, str(exc)

        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            return None, message or f"snmpwalk exited with code {proc.returncode}"

        return self._parse_if_names(proc.stdout), None

    @staticmethod
    def _parse_if_names(stdout: str) -> Dict[int, str]:
        """
        Parse snmpwalk -O nq output, e.g.
            .1.3.6.1.2.1.2.2.1.2.18 "lte0"
        into {18: "lte0"}. Lines that do not match are skipped.
        """

        names: Dict[int, str] = {}

        for line in stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue

            oid, value = parts
            try:
                index = int(oid.rsplit(".", 1)[1])
            except (IndexError, ValueError):
                continue

            names[index] = value.strip().strip('"')

        return names

    def _read_counters(
        self,
    ) -> Tuple[Optional[Tuple[int, int]], Optional[int], bool, Optional[str]]:
        """
        Read the in/out counters, choosing 64-bit or 32-bit on first use.

        Returns (counters, bits, replied, error_detail):
          counters     (in_octets, out_octets), or None on failure
          bits         64 or 32, or None on failure
          replied      True if the agent sent any reply
          error_detail why no counters were read, or None
        """

        # Already know which counters this agent provides.
        if self._counter_bits is not None:
            return self._try_counters(self._counter_bits)

        # First use: try 64-bit counters.
        counters, bits, replied, error_detail = self._try_counters(64)

        # Worked, or no reply at all (no point trying 32-bit as well).
        if counters is not None or not replied:
            if counters is not None:
                self._counter_bits = 64
            return counters, bits, replied, error_detail

        # The agent replied, but without 64-bit counters (it answers
        # "No Such Object"), so fall back to the 32-bit counters.
        counters, bits, replied, error_detail = self._try_counters(32)
        if counters is not None:
            self._counter_bits = 32

        return counters, bits, replied, error_detail

    def _try_counters(
        self,
        bits: int,
    ) -> Tuple[Optional[Tuple[int, int]], Optional[int], bool, Optional[str]]:
        """Request one counter size and parse it (see _read_counters)."""

        stdout, error_detail = self._run_snmpget_subprocess(bits)

        if stdout is None:
            return None, None, False, error_detail

        counters = self._parse_counters(stdout)

        if counters is None:
            return None, None, True, self._describe_bad_reply(stdout)

        return counters, bits, True, None

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

    def _run_snmpget_subprocess(
        self,
        bits: int,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Run one snmpget request for both interface counters.

        bits: 64 for ifHCInOctets/ifHCOutOctets, 32 for ifIn/OutOctets.
        Both OIDs are requested in one SNMP request.

        Returns (stdout, None) if the agent replied, or
        (None, error message) if there was no reply or snmpget failed.
        """

        in_base, out_base = COUNTER_OIDS[bits]
        in_oid = f"{in_base}.{self.if_index}"
        out_oid = f"{out_base}.{self.if_index}"

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
            return None, f"snmpget did not finish within {self.timeout_s + 1.0:g} s"
        except OSError as exc:
            # e.g. snmpget is not installed
            return None, str(exc)

        if proc.returncode != 0:
            # e.g. "Timeout: No Response from 127.0.0.1." (no agent,
            # wrong community string, or agent not reachable)
            message = (proc.stderr or proc.stdout).strip()
            return None, message or f"snmpget exited with code {proc.returncode}"

        return proc.stdout, None

    @staticmethod
    def _describe_bad_reply(stdout: str) -> str:
        """Explain a reply that held no usable counters."""

        first_line = next(
            (line.strip() for line in stdout.splitlines() if line.strip()),
            "empty reply",
        )

        # The agent answers this when the interface number does not exist.
        if "No Such Instance" in first_line:
            return f"{first_line} (check if_index)"

        return first_line

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
        output isn't what we expected, and we fail loudly (return None)
        rather than silently guessing which lines are the values we want.
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

        # A negative counter difference can happen if a 32-bit counter
        # wraps around or the network device resets its counters.
        # In this case, the rate cannot be trusted.
        if delta_in < 0 or delta_out < 0:
            return None, None

        in_bps = round((delta_in * 8) / elapsed_s, 2)
        out_bps = round((delta_out * 8) / elapsed_s, 2)

        return in_bps, out_bps


if __name__ == "__main__":
    # Manual test: perform five measurements at five-second intervals.
    # The deployment interval is 30 seconds by default.
    # Target, interface and community come from config.json.
    target, config_if_index, community = load_snmp_config()
    config_if_name = load_snmp_interface_name()

    parser = argparse.ArgumentParser(description="Poll SNMP interface counters.")
    parser.add_argument(
        "if_index",
        nargs="?",
        type=int,
        default=config_if_index,
        help=f"interface number to poll (default from config.json: {config_if_index}); "
             "ignored when an interface name is set",
    )
    parser.add_argument(
        "--interface",
        default=config_if_name,
        help=f"interface NAME to poll, e.g. lte0 (default from config.json: {config_if_name})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between polls (default: 5; keep at 3 or more, "
             "the agent caches counter values for about 3 s)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="number of polls before stopping (default: 5)",
    )
    args = parser.parse_args()

    poller = SNMPPoller(
        target=target,
        if_index=args.if_index,
        community=community,
        if_name=args.interface,
    )

    poller.run_continuous(
        interval_s=args.interval,
        stop_after=args.count,
    )