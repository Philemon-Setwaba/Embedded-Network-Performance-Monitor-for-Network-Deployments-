"""
src/measurement/traceroute.py

Shows the path (hops) from the Pi to a target using the system
`traceroute` command. Ping shows THAT there is a problem; traceroute
shows WHERE along the path replies stop.

Usage:
    python3 src/measurement/traceroute.py            # traces to 8.8.8.8
    python3 src/measurement/traceroute.py 1.1.1.1    # traces to 1.1.1.1
"""

import argparse
import subprocess

# Target used when none is given on the command line.
DEFAULT_TARGET = "8.8.8.8"

# Safety limit so a stuck run cannot hang forever.
RUN_TIMEOUT_S = 60.0


def run_traceroute(target: str) -> str:
    """Run traceroute to the target and return its output as text."""

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
        return f"traceroute to {target} did not finish within {RUN_TIMEOUT_S:g} s"
    except OSError as exc:
        # e.g. traceroute is not installed. Return a message instead of
        # crashing, so the other measurements keep running.
        return f"traceroute could not run: {exc}"

    return proc.stdout + proc.stderr


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run traceroute to a target.")
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_TARGET,
        help=f"host or IP address (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args()

    print(run_traceroute(args.target))