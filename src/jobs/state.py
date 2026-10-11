"""
src/jobs/state.py

The EVENT RULES of the monitor. It does not measure anything itself.
It reads the LATEST results of other jobs, decides the state, and:
  - stores the state as metrics (so Grafana can show a state timeline)
  - prints ONE line, only when the state CHANGES

There are TWO separate states, because both can be true at the same time
("backhaul up, but the link is full"):

===========================================================================
1. BACKHAUL STATE  (StateJob, active lane, once per new ping)
===========================================================================

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

Why 2 checks in a row: one failed check can be one bad moment; at a 10 s
ping interval, 2 in a row means ~20 s of real outage before "down".
This number is a project choice.

===========================================================================
2. LINK STATE  (LinkStateJob, passive lane, once per new SNMP reading)
===========================================================================

Answers: "is the network slow because the link is full?"

States (monitor_link_state):
   0  link normal       the last FULL_AFTER_READINGS (6) readings were all
                        below the line
   1  link full         the last 6 readings were all at or above the line
  -1  not judged        backhaul not up, no SNMP reply, no bps yet,
                        or not enough readings yet after start-up

A reading is "full" if:
    download (in_bps)  >= 70 % of 10 Mbit/s = 7.0 Mbit/s   OR
    upload   (out_bps) >= 70 % of  5 Mbit/s = 3.5 Mbit/s

Where the numbers come from:
  - 10 / 5 Mbit/s: the iNethi uplink (Phokeer et al. 2020), our reference link.
  - 70 %: project choice. Queueing theory (M/M/1, Kleinrock 1975) says the
    average delay grows like 1 / (1 - utilisation): 3.3x at 70 %, 5x at 80 %,
    10x at 90 %, so delay starts to climb steeply above ~70 %.
  - 6 readings in a row = 1 minute at 10 s: project choice. A single download
    can fill the link for 10-30 s and users barely notice; full for a whole
    minute means something is really using the link.
  - "Back to normal" also needs 6 readings in a row, so the state does not
    flip on and off.

Only judged while the backhaul is UP: when it is down, nothing gets through,
so "full" makes no sense.

Also stored: monitor_link_full_streak = full readings in a row (0, 1, 2 ...).

Limit: SNMP reads the Pi's own wlan0 here (no router available), so it only
sees the Pi's own traffic. In a real deployment the SNMP target is the router.
Not yet done: skip readings taken during the monitor's OWN iperf3 test
(added when the iperf3 job is written).
"""

import json
from typing import Callable, Optional

from src.measurement import ping
from src.reporting import line_format, victoria
from src.jobs.job_config import CONFIG_PATH


# ===========================================================================
# 1. BACKHAUL STATE
# ===========================================================================

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


# ===========================================================================
# 2. LINK STATE
# ===========================================================================

# State codes stored in monitor_link_state.
LINK_NORMAL = 0
LINK_FULL = 1
LINK_NOT_JUDGED = -1

LINK_NAMES = {
    LINK_NORMAL: "link normal",
    LINK_FULL: "link full",
    LINK_NOT_JUDGED: "link not judged",
}

# Used when config.json has no "link" section (or no setting in it).
DEFAULT_DOWN_CAPACITY_BPS = 10_000_000   # iNethi uplink, download (Phokeer et al. 2020)
DEFAULT_UP_CAPACITY_BPS = 5_000_000      # iNethi uplink, upload
DEFAULT_FULL_PCT = 70                    # project choice (see top of file)
DEFAULT_FULL_AFTER_READINGS = 6          # 6 x 10 s = 1 minute (project choice)


def _load_link_settings():
    """Read the "link" section of config.json."""
    try:
        with open(CONFIG_PATH) as f:
            section = json.load(f).get("link", {})
    except FileNotFoundError:
        section = {}
    return (float(section.get("down_capacity_bps", DEFAULT_DOWN_CAPACITY_BPS)),
            float(section.get("up_capacity_bps", DEFAULT_UP_CAPACITY_BPS)),
            float(section.get("full_pct", DEFAULT_FULL_PCT)),
            int(section.get("readings", DEFAULT_FULL_AFTER_READINGS)))


class LinkStateJob:
    """Decides "link normal / link full" once per new SNMP reading."""

    name = "link_state"

    def __init__(self, snmp_job, backhaul_job, log: Callable[[str], None] = print):
        self.snmp_job = snmp_job
        self.backhaul_job = backhaul_job    # the StateJob (other lane) - we only read .state
        self.log = log

        down_cap, up_cap, self.full_pct, self.readings = _load_link_settings()
        # The lines, in bits per second.
        self.down_line_bps = down_cap * self.full_pct / 100     # 7.0 Mbit/s
        self.up_line_bps = up_cap * self.full_pct / 100         # 3.5 Mbit/s

        # Same interval as SNMP: the lane checks us right after SNMP.
        self.interval_s = snmp_job.interval_s

        self.state = LINK_NOT_JUDGED
        self.full_streak = 0                # full readings in a row
        self.normal_streak = 0              # not-full readings in a row
        self._judged_reading: Optional[str] = None   # timestamp of the SNMP reading we judged

        self.latest_push_error: Optional[str] = None

    def should_run(self) -> bool:
        """Only once per NEW SNMP reading."""
        r = self.snmp_job.latest
        return r is not None and r.timestamp != self._judged_reading

    def _decide(self):
        """Return (new_state, reason) from the latest SNMP reading."""
        r = self.snmp_job.latest

        # Cannot judge: backhaul not up, no SNMP reply, or no bps yet.
        if self.backhaul_job.state != STATE_UP:
            why = f"backhaul state is '{STATE_NAMES.get(self.backhaul_job.state, '?')}'"
        elif not r.reachable:
            why = "no SNMP reply"
        elif r.in_bps is None or r.out_bps is None:
            why = "no bps yet (first reading)"
        else:
            why = None
        if why is not None:
            self.full_streak = 0
            self.normal_streak = 0
            return LINK_NOT_JUDGED, why

        # Is THIS reading full?
        down_full = r.in_bps >= self.down_line_bps
        up_full = r.out_bps >= self.up_line_bps
        if down_full or up_full:
            self.full_streak += 1
            self.normal_streak = 0
        else:
            self.normal_streak += 1
            self.full_streak = 0

        why = (f"in {r.in_bps / 1e6:.2f} Mbit/s (line {self.down_line_bps / 1e6:g}), "
               f"out {r.out_bps / 1e6:.2f} Mbit/s (line {self.up_line_bps / 1e6:g}), ")

        if self.full_streak >= self.readings:
            return LINK_FULL, why + f"{self.full_streak} full readings in a row"
        if self.normal_streak >= self.readings:
            return LINK_NORMAL, why + f"{self.normal_streak} normal readings in a row"
        return self.state, why + "not confirmed yet"   # keep the old state

    def run(self) -> int:
        """Judge the latest SNMP reading, store the state, print if it changed. Never raises."""
        r = self.snmp_job.latest
        self._judged_reading = r.timestamp

        new_state, why = self._decide()
        if new_state != self.state:
            self.log(f"STATE  {LINK_NAMES[self.state]} -> {LINK_NAMES[new_state]}  ({why})")
        self.state = new_state

        # Store with the SNMP reading's time, so it lines up with the bps in Grafana.
        ts = line_format.to_ms(r.timestamp)
        labels = {"device": line_format.DEVICE}
        lines = [
            line_format.line("monitor_link_state", labels, self.state, ts),
            line_format.line("monitor_link_full_streak", labels, self.full_streak, ts),
        ]
        self.latest_push_error = victoria.push(lines)
        return self.state


# ===========================================================================
# Manual test with FAKE results (no network needed, nothing is measured).
# Nothing is sent to VictoriaMetrics: push is replaced by a dummy here,
# so the fake states never mix with real data.
# ===========================================================================

def _test_backhaul():
    import types
    from src.measurement import tcp_reachability as tr

    print("=== BACKHAUL STATE ===")
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


def _test_link():
    import types

    print("\n=== LINK STATE ===")
    fake_snmp = types.SimpleNamespace(latest=None, interval_s=10.0)
    fake_backhaul = types.SimpleNamespace(state=STATE_UP)
    job = LinkStateJob(fake_snmp, fake_backhaul, log=lambda m: print("    LOG:", m))
    print(f"lines: in >= {job.down_line_bps / 1e6:g} Mbit/s or out >= "
          f"{job.up_line_bps / 1e6:g} Mbit/s, {job.readings} readings in a row\n")

    M = 1_000_000
    steps = (
        # label,            backhaul,   reachable, in_bps,  out_bps
        [("first reading",  STATE_UP,   True,      None,    None)]
        + [("quiet",        STATE_UP,   True,      0.2 * M, 0.1 * M)] * 6
        + [("download 8M",  STATE_UP,   True,      8.0 * M, 0.3 * M)] * 6
        + [("quiet once",   STATE_UP,   True,      0.2 * M, 0.1 * M)]
        + [("upload 4M",    STATE_UP,   True,      0.5 * M, 4.0 * M)]
        + [("quiet",        STATE_UP,   True,      0.2 * M, 0.1 * M)] * 6
        + [("backhaul down", STATE_DOWN, True,     0.0,     0.0)]
        + [("no SNMP reply", STATE_UP,  False,     None,    None)]
    )
    for n, (label, bh, ok, in_bps, out_bps) in enumerate(steps, start=1):
        fake_backhaul.state = bh
        fake_snmp.latest = types.SimpleNamespace(
            timestamp=f"2026-10-11T08:{n // 6:02d}:{(n % 6) * 10:02d}+00:00",
            reachable=ok, in_bps=in_bps, out_bps=out_bps)
        state = job.run()
        print(f"{n:>2} {label:<15} -> {state:>2} {LINK_NAMES[state]:<16} "
              f"full streak {job.full_streak}")


if __name__ == "__main__":
    victoria.push = lambda lines: None      # test only: do NOT store fake states
    _test_backhaul()
    _test_link()