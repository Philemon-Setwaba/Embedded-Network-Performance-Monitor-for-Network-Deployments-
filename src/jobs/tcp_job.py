"""
src/jobs/tcp_job.py

The TCP check job - the SECOND OPINION when ping fails.
It answers: "is the backhaul really down, or is only ping being blocked?"

When it runs it does ONE check:

    measure (tcp_reachability.py) -> translate (tcp_lines.py) -> send (victoria.py)

and keeps the latest result, so state.py can read it later (event B1).

When it runs - NOT on its own timer:
  - only RIGHT AFTER a ping that got NO REPLY (ping error "no_reply")
  - it runs in the active lane, on the same grid as ping and listed after
    it, so it never overlaps with ping
  - NOT when ping says "Network is unreachable" (ping error "ping_error"):
    then the Pi itself has no network (local link down), TCP could not get
    out either, so it would add nothing
  - at most ONCE per failed ping (it remembers which ping it answered)

Why TCP to 1.1.1.1:443:
  - port 443 (HTTPS) is almost never blocked, so it still works where ICMP
    (ping) is filtered or rate-limited
  - a different company (Cloudflare) from the ping target (Google), so a
    problem at one of them is not mistaken for a backhaul failure

  ping       TCP check     meaning
  reply      (not run)     backhaul up
  no reply   connected     backhaul up - ping is blocked somewhere
  no reply   timeout       backhaul probably down (2 in a row -> "down")

Cost: one connection, ~9 ms when it works, at most timeout_s (3 s) when not.

Settings: the "tcp" section of config.json, e.g.
    "tcp": {"target": "1.1.1.1", "port": 443, "timeout_s": 3}
"""

import json
from typing import Optional

# measure
from src.measurement import tcp_reachability
# translate
from src.reporting import tcp_lines
# send
from src.reporting import victoria
# we need ping's error names, and config.json's location
from src.measurement import ping
from src.jobs.job_config import CONFIG_PATH


# Used when config.json has no "tcp" section (or no setting in it).
DEFAULT_TARGET = tcp_reachability.DEFAULT_TARGET        # "1.1.1.1"
DEFAULT_PORT = tcp_reachability.DEFAULT_PORT            # 443
DEFAULT_TIMEOUT_S = tcp_reachability.DEFAULT_TIMEOUT_S  # 3.0


def _load_tcp_settings():
    """Read "target", "port" and "timeout_s" from the "tcp" section of config.json."""
    try:
        with open(CONFIG_PATH) as f:
            section = json.load(f).get("tcp", {})
    except FileNotFoundError:
        section = {}
    return (section.get("target", DEFAULT_TARGET),
            int(section.get("port", DEFAULT_PORT)),
            float(section.get("timeout_s", DEFAULT_TIMEOUT_S)))


class TcpJob:
    """One TCP check, but only when the latest ping got no reply."""

    name = "tcp"

    def __init__(self, ping_job):
        # The ping job whose results we react to.
        self.ping_job = ping_job

        self.target, self.port, self.timeout_s = _load_tcp_settings()

        # Same interval as ping, so the lane checks us in the same round,
        # right after ping. should_run() then decides if we actually run.
        self.interval_s = ping_job.interval_s

        # The timestamp of the ping we last answered, so we run at most once
        # per failed ping.
        self._answered_ping: Optional[str] = None

        # The latest result and push error (None = no error), for state.py.
        self.latest: Optional[tcp_reachability.TCPReachabilityResult] = None
        self.latest_push_error: Optional[str] = None

    def should_run(self) -> bool:
        """True only if the latest ping got NO REPLY and we have not answered it yet."""
        last_ping = self.ping_job.latest
        if last_ping is None:
            return False                                  # no ping yet
        if last_ping.error != ping.ERROR_NO_REPLY:
            return False                                  # ping worked, or local problem
        return last_ping.timestamp != self._answered_ping  # not answered yet

    def run(self) -> tcp_reachability.TCPReachabilityResult:
        """Do one check: measure -> translate -> send. Never raises."""

        self._answered_ping = self.ping_job.latest.timestamp if self.ping_job.latest else None

        result = tcp_reachability.check_reachability(       # 1. measure
            self.target, self.port, self.timeout_s)
        lines = tcp_lines.to_lines(result)                   # 2. translate
        push_error = victoria.push(lines)                    # 3. send

        self.latest = result                                 # 4. remember
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: show what should_run() says for 3 kinds of ping result,
    # then run one real check by hand.
    from src.jobs.ping_job import PingJob

    ping_job = PingJob()
    job = TcpJob(ping_job)
    print(f"TCP job: target={job.target}:{job.port}, timeout {job.timeout_s:g} s, "
          f"checked every {job.interval_s:g} s (with ping)\n")

    t = "2026-10-11T10:00:00+00:00"
    cases = [
        ("ping ok", ping.PingResult(t, "8.8.8.8", True, 21.7, 0.0)),
        ("ping no_reply", ping.PingResult(t, "8.8.8.8", False, None, 100.0,
                                          error=ping.ERROR_NO_REPLY)),
        ("ping Network is unreachable", ping.PingResult(t, "8.8.8.8", False, None, 100.0,
                                                         error=ping.ERROR_PING_ERROR)),
    ]
    for label, fake in cases:
        ping_job.latest = fake
        job._answered_ping = None
        print(f"  {label:<30} -> should_run = {job.should_run()}")

    # Leave the "no_reply" ping as the latest, and answer it once.
    ping_job.latest = cases[1][1]
    job._answered_ping = None
    print("\nOne real check (as if the ping got no reply):")
    r = job.run()
    print(r)
    for line in tcp_lines.to_lines(r):
        print("   ", line)
    print("    -> pushed" if job.latest_push_error is None
          else f"    -> push FAILED: {job.latest_push_error}")
    print(f"  same ping again -> should_run = {job.should_run()}  (already answered)")