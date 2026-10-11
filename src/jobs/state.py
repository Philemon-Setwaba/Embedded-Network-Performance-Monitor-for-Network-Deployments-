"""
src/jobs/state.py

The EVENT RULES of the monitor (the backhaul).

It does not measure anything itself. Each round it reads the LATEST results
of the ping, TCP and Wi-Fi jobs, decides which state the monitor is in, and:
  - stores the state as metrics (so Grafana can show a state timeline)
  - prints ONE line, only when the state CHANGES

States (monitor_backhaul_state):
   0  backhaul up       ping got >= 1 reply, OR the TCP check got through
   1  backhaul down     ping got no reply AND TCP did not get through,
                        DOWN_AFTER_CHECKS (2) checks in a row
   2  local link down   the Pi's own Wi-Fi is not connected, OR ping says
                        "Network is unreachable" - the Pi itself has no
                        network, so the backhaul CANNOT be judged
  -1  unknown           at start-up, until the first check decides

Rules, in this order (local link down is checked FIRST):
  1. Wi-Fi not connected, or ping "Network is unreachable"  -> local link down
  2. ping got a reply                                        -> backhaul up
  3. ping no reply, TCP connected/refused (got through)      -> backhaul up
                                                                (ping blocked)
  4. ping no reply, TCP did not get through                  -> one more FAILED
                                                                check; down after 2
  5. anything else (e.g. ping could not run)                 -> keep the old state
  "Back up" needs only 1 good check.

Also stored: monitor_backhaul_fail_streak = failed checks in a row (0, 1, 2 ...).
It shows "something is wrong, not confirmed yet" after the FIRST failed check,
while the state itself still waits for the second.

Why 2 checks in a row: one failed check can be one bad moment; at a 10 s
ping interval, 2 in a row means ~20 s of real outage before "down".
This number is a project choice.

Why there is NO "link full" state here: how full the link is (% of
capacity) is worked out in Grafana from the raw snmp_in_bps / snmp_out_bps,
with colour bands and a "full for 1 minute" check (min_over_time). Nothing
on the Pi acts on "link full", and doing it in Grafana lets the threshold be
changed at any time and re-judge past data too. The backhaul state stays
HERE because the monitor itself acts on it (traceroute, iperf3,
store-and-forward).

It runs in the active lane, right after ping and tcp, once per new ping.
"""

from typing import Callable, Optional

from src.measurement import ping
from src.reporting import line_format, victoria


# State codes stored in monitor_backhaul_state.
STATE_UP = 0
STATE_DOWN = 1
STATE_LOCAL_DOWN = 2
STATE_UNKNOWN = -1

STATE_NAMES = {
    STATE_UP: "backhaul up",
    STATE_DOWN: "backhaul down",
    STATE_LOCAL_DOWN: "local link down",
    STATE_UNKNOWN: "unknown",
}

# How many failed checks in a row before "backhaul down" (project choice).
DOWN_AFTER_CHECKS = 2

# ping's own message when the Pi has no network at all.
NETWORK_UNREACHABLE_TEXT = "Network is unreachable"


class StateJob:
    """Applies the backhaul rules once per new ping result."""

    name = "state"

    def __init__(self, ping_job, tcp_job, wifi_job, log: Callable[[str], None] = print):
        self.ping_job = ping_job
        self.tcp_job = tcp_job
        self.wifi_job = wifi_job
        self.log = log                      # how to print a state change

        # Same interval as ping: the lane checks us right after ping and tcp.
        self.interval_s = ping_job.interval_s

        self.state = STATE_UNKNOWN
        self.fail_streak = 0
        self._judged_ping: Optional[str] = None   # timestamp of the ping we judged

        self.latest_push_error: Optional[str] = None

    def should_run(self) -> bool:
        """Only once per NEW ping result."""
        p = self.ping_job.latest
        return p is not None and p.timestamp != self._judged_ping

    # -----------------------------------------------------------------------
    # The rules
    # -----------------------------------------------------------------------

    def _decide(self):
        """Return (new_state, reason) from the latest ping, TCP and Wi-Fi results."""
        p = self.ping_job.latest
        wifi = self.wifi_job.latest

        # TCP result only counts if it answered THIS ping.
        tcp = self.tcp_job.latest
        tcp_for_this_ping = (tcp is not None
                             and self.tcp_job._answered_ping == p.timestamp)

        # 1. Local link down - checked first.
        wifi_down = wifi is not None and not wifi.connected
        unreachable = (p.error == ping.ERROR_PING_ERROR
                       and NETWORK_UNREACHABLE_TEXT in (p.error_detail or ""))
        if wifi_down or unreachable:
            self.fail_streak = 0            # the backhaul cannot be judged now
            why = "Wi-Fi not connected" if wifi_down else "ping: Network is unreachable"
            return STATE_LOCAL_DOWN, why

        # 2. Ping got a reply.
        if p.reachable:
            self.fail_streak = 0
            return STATE_UP, f"ping ok ({p.rtt_ms} ms, loss {p.loss_pct:g} %)"

        # 3./4. Ping got no reply: the TCP check decides.
        if p.error == ping.ERROR_NO_REPLY:
            if tcp_for_this_ping and tcp.reachable:
                self.fail_streak = 0
                return STATE_UP, f"ping no reply, but TCP {tcp.status} (ping blocked?)"

            self.fail_streak += 1
            tcp_text = tcp.status if tcp_for_this_ping else "not run"
            why = (f"ping no reply + TCP {tcp_text}, "
                   f"{self.fail_streak} check(s) in a row")
            if self.fail_streak >= DOWN_AFTER_CHECKS:
                return STATE_DOWN, why
            return self.state, why          # not confirmed yet: keep the old state

        # 5. Anything else (ping timeout, no_rtt, exec_failed, other ping_error):
        #    the monitor could not judge - keep the old state.
        return self.state, f"ping could not judge ({p.error})"

    # -----------------------------------------------------------------------
    # One round
    # -----------------------------------------------------------------------

    def run(self) -> int:
        """Judge the latest ping, store the state, print if it changed. Never raises."""
        p = self.ping_job.latest
        self._judged_ping = p.timestamp

        new_state, why = self._decide()
        if new_state != self.state:
            self.log(f"STATE  {STATE_NAMES[self.state]} -> {STATE_NAMES[new_state]}  ({why})")
        self.state = new_state

        # Store with the PING's time, so the state lines up with the ping in Grafana.
        ts = line_format.to_ms(p.timestamp)
        labels = {"device": line_format.DEVICE}
        lines = [
            line_format.line("monitor_backhaul_state", labels, self.state, ts),
            line_format.line("monitor_backhaul_fail_streak", labels, self.fail_streak, ts),
        ]
        self.latest_push_error = victoria.push(lines)
        return self.state


if __name__ == "__main__":
    # Manual test with FAKE results (no network needed, nothing is measured):
    # walks through every rule once and prints the state after each step.
    # Nothing is sent to VictoriaMetrics: push is replaced by a dummy here,
    # so the fake states never mix with real data.
    import types
    from src.measurement import tcp_reachability as tr

    victoria.push = lambda lines: None      # test only: do NOT store fake states

    fake_ping = types.SimpleNamespace(latest=None, interval_s=10.0)
    fake_tcp = types.SimpleNamespace(latest=None, _answered_ping=None)
    fake_wifi = types.SimpleNamespace(latest=types.SimpleNamespace(connected=True))
    job = StateJob(fake_ping, fake_tcp, fake_wifi, log=lambda m: print("    LOG:", m))

    steps = [
        # label,                         ping ok, ping error, error detail, tcp, wifi
        ("ping ok",                       True,  None, None, None, True),
        ("no reply + TCP timeout",        False, ping.ERROR_NO_REPLY, None, "timeout", True),
        ("no reply + TCP timeout",        False, ping.ERROR_NO_REPLY, None, "timeout", True),
        ("no reply + TCP connected",      False, ping.ERROR_NO_REPLY, None, "connected", True),
        ("Wi-Fi off, Network unreachable", False, ping.ERROR_PING_ERROR,
         "ping: connect: Network is unreachable", None, False),
        ("ping ok again",                 True,  None, None, None, True),
    ]
    for n, (label, ok, err, detail, tcp_status, wifi_ok) in enumerate(steps, start=1):
        ts = f"2026-10-11T07:28:{n:02d}+00:00"
        fake_ping.latest = ping.PingResult(ts, "8.8.8.8", ok, 21.0 if ok else None,
                                           0.0 if ok else 100.0, error=err, error_detail=detail)
        fake_wifi.latest.connected = wifi_ok
        if tcp_status:
            fake_tcp.latest = tr.TCPReachabilityResult(ts, "1.1.1.1", 443,
                                                       tcp_status == "connected", None, tcp_status)
            fake_tcp._answered_ping = ts
        state = job.run()
        print(f"{label:<32} -> {state:>2} {STATE_NAMES[state]:<16} streak {job.fail_streak}")