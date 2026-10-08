"""
src/measurement/traceroute.py

Shows the path (hops) from the Pi to a target using the system
`traceroute` command. Ping shows THAT there is a problem; traceroute
shows WHERE along the path replies stop.

The text traceroute prints is read into a TracerouteResult. Every probe
time is kept exactly as traceroute printed it (no averaging), and the full
text is kept in raw_output, so nothing traceroute printed is lost.

Real output this was written from (Pi on eduroam, 2026-10-08):
    traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets
     1  196.24.152.2  5.714 ms  5.791 ms  7.001 ms
     6  155.232.128.76  31.528 ms * *
     8  * * *
     9  172.253.73.109  20.063 ms 192.178.86.209  19.717 ms 8.8.8.8  20.138 ms

Things to know when reading traceroute (Danych et al., 2025):
  - "* * *" does NOT mean the path is broken: routers may ignore probes on
    purpose (hop 8 above, yet 8.8.8.8 was still reached at hop 9).
  - one hop can show several IPs (hop 9 above): load balancing sends each
    probe a different way, so every probe keeps its own IP.

Usage:
    python3 src/measurement/traceroute.py            # traces to 8.8.8.8
    python3 src/measurement/traceroute.py 1.1.1.1    # traces to 1.1.1.1
"""

import argparse
import dataclasses
import re
import subprocess
from datetime import datetime, timezone
from typing import List, Optional

# Target used when none is given on the command line.
DEFAULT_TARGET = "8.8.8.8"

# Safety limit so a stuck run cannot hang forever.
RUN_TIMEOUT_S = 60.0

# Possible outcomes, stored in TracerouteResult.status
STATUS_REACHED = "reached"            # a probe got a reply from the target itself
STATUS_NOT_REACHED = "not_reached"    # ran, but the target never replied
STATUS_BLOCKED = "blocked"            # a router replied "blocked/unreachable" (!X, !H, ...)
STATUS_COULD_NOT_RUN = "could_not_run"  # traceroute did not run or did not finish

# First line, e.g. "traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets"
# The address in brackets is the target's IP (also when a name was given).
HEADER_PATTERN = re.compile(r"^traceroute to \S+ \(([^)]+)\)")

# A hop line starts with the hop number, e.g. " 9  172.253.73.109  20.063 ms ..."
HOP_LINE_PATTERN = re.compile(r"^\s*(\d+)\s+(.*)$")


@dataclasses.dataclass
class TracerouteProbe:
    """One probe (traceroute sends 3 per hop)."""
    ip: Optional[str]                  # who replied; None for "*"
    rtt_ms: Optional[float]            # time as printed; None for "*"
    annotation: Optional[str] = None   # e.g. "!X" (blocked), "!H" (host unreachable)


@dataclasses.dataclass
class TracerouteHop:
    """One line of traceroute output."""
    hop: int
    probes: List[TracerouteProbe]


@dataclasses.dataclass
class TracerouteResult:
    """Stores one traceroute run."""
    timestamp: str
    target: str
    target_ip: Optional[str]           # from the first line; None if it did not run
    status: str                        # one of the STATUS_* values above
    hops: List[TracerouteHop]
    raw_output: str                    # everything traceroute printed, unchanged
    error_detail: Optional[str] = None # why it could not run


def run_traceroute(target: str = DEFAULT_TARGET) -> TracerouteResult:
    """Run traceroute to the target and return the result."""

    timestamp = datetime.now(timezone.utc).isoformat()

    def failed(message: str, raw: str = "") -> TracerouteResult:
        return TracerouteResult(timestamp, target, None, STATUS_COULD_NOT_RUN,
                                [], raw, error_detail=message)

    # `-n` prints IP addresses only (no DNS lookups), which is faster and
    # still works when DNS is unavailable during an outage.
    try:
        proc = subprocess.run(
            ["traceroute", "-n", target],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return failed(f"traceroute to {target} did not finish within {RUN_TIMEOUT_S:g} s")
    except OSError as exc:
        # e.g. traceroute is not installed. Return a result instead of
        # crashing, so the other measurements keep running.
        return failed(f"traceroute could not run: {exc}")

    raw = proc.stdout + proc.stderr

    # No first line = traceroute did not start, e.g. unknown host name.
    header = HEADER_PATTERN.search(raw)
    if not header:
        return failed(raw.strip() or f"traceroute exited with code {proc.returncode}", raw)

    target_ip = header.group(1)
    hops = _parse_hops(raw)

    # Work out the status from the probes.
    all_probes = [p for h in hops for p in h.probes]
    if any(p.ip == target_ip for p in all_probes):
        status = STATUS_REACHED
    elif any(p.annotation for p in all_probes):
        status = STATUS_BLOCKED
    else:
        status = STATUS_NOT_REACHED

    return TracerouteResult(timestamp, target, target_ip, status, hops, raw)


def _parse_hops(raw: str) -> List[TracerouteHop]:
    """
    Read every hop line. Words on a hop line are one of:
        an IP address  -> the router that sent the next times
        a time + "ms"  -> one probe answered, from the last IP seen
        "!X", "!H" ... -> note on the probe just before it
        "*"            -> one probe got no reply
    """
    hops = []
    for line in raw.splitlines():
        match = HOP_LINE_PATTERN.match(line)
        if not match:
            continue                       # the first line, or an empty line

        hop_number = int(match.group(1))
        words = match.group(2).split()
        probes: List[TracerouteProbe] = []
        current_ip: Optional[str] = None

        i = 0
        while i < len(words):
            word = words[i]
            if word == "*":
                probes.append(TracerouteProbe(ip=None, rtt_ms=None))
            elif word.startswith("!"):
                if probes:
                    probes[-1].annotation = word
            elif i + 1 < len(words) and words[i + 1] == "ms":
                probes.append(TracerouteProbe(ip=current_ip, rtt_ms=float(word)))
                i += 1                     # skip the "ms"
            else:
                current_ip = word          # an IP address
            i += 1

        hops.append(TracerouteHop(hop_number, probes))
    return hops


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run traceroute to a target.")
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_TARGET,
        help=f"host or IP address (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args()

    result = run_traceroute(args.target)
    print(result.raw_output)
    print("status:", result.status, "| target_ip:", result.target_ip,
          "| error:", result.error_detail)
    for hop in result.hops:
        print(hop)