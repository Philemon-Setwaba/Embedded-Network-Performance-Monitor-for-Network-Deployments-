"""
src/jobs/iperf_job.py

The iperf3 job: throughput (TCP) and jitter / loss (UDP) to the iperf3 server.

    measure (iperf.py) -> translate (iperf_lines.py) -> send (victoria.py)

iperf3 SENDS A LOT of traffic, so it only runs when it cannot hurt users
and its own traffic cannot spoil the result:

  test                 when                                         cost
  TCP upload +         once per NIGHT (00:00-05:00 SAST), backhaul  ~18 MB per night
  TCP download (-R)    up, link QUIET for the last 5 min:            (user's earlier note)
                       all 30 SNMP readings < 1 Mbit/s down
                       and < 0.5 Mbit/s up
  UDP 500 kbit/s       at most once per HOUR, backhaul up, link     ~0.6 MB per run,
                       not busy for the last 1 min: all 6 SNMP      ~15 MB/day at most
                       readings < 7 Mbit/s down and < 3.5 Mbit/s
                       up (70 % of the iNethi 10 / 5 Mbit/s line)

  - a TCP night only counts as DONE when the test worked; if it failed
    (e.g. server not reachable) it is tried again, at most once per hour,
    inside the same night window
  - a UDP attempt counts whether it worked or not (no retry storm)
  - never while backhaul down / local link down / unknown
  - 2 s pause between tests (iperf.PAUSE_BETWEEN_TESTS_S: measured -
    a test started right after another was refused by the server)

All numbers above (window, quiet lines, hourly UDP) are PROJECT CHOICES.

Own traffic marker: right before and right after each test it stores
    iperf_test_active{test} 1   (start)
    iperf_test_active{test} 0   (end)
so Grafana can leave the monitor's OWN iperf3 traffic out of "link use".

While a run is going, the active lane is busy (ping waits): about
10 + 2 + 10 s for the TCP pair, 10 s for UDP (plus iperf3 start-up).

Settings: the "iperf3" section of config.json, e.g.
    "iperf3": {"server": "196.42.104.92", "port": 5201,
               "night_start_hour": 0, "night_end_hour": 5, "udp_interval_s": 3600}
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

# measure
from src.measurement import iperf
# translate
from src.reporting import iperf_lines, line_format
# send
from src.reporting import victoria
# backhaul state codes, config.json location
from src.jobs import state
from src.jobs.job_config import CONFIG_PATH


# South Africa Standard Time = UTC+2 all year (South Africa has no daylight saving).
SAST = timezone(timedelta(hours=2), "SAST")

# Used when config.json has no such setting.
DEFAULT_NIGHT_START_HOUR = 0        # 00:00 SAST
DEFAULT_NIGHT_END_HOUR = 5          # until 05:00 SAST (not included)
DEFAULT_UDP_INTERVAL_S = 3600.0     # at most once per hour
TCP_RETRY_S = 3600.0                # a failed TCP night: try again after 1 h

# "Quiet" for TCP: the last 5 min (30 readings x 10 s) below these.
TCP_QUIET_READINGS = 30
TCP_QUIET_DOWN_BPS = 1_000_000      # 1 Mbit/s
TCP_QUIET_UP_BPS = 500_000          # 0.5 Mbit/s

# "Not busy" for UDP: the last 1 min (6 readings) below 70 % of 10 / 5 Mbit/s.
UDP_QUIET_READINGS = 6
UDP_QUIET_DOWN_BPS = 7_000_000      # 7 Mbit/s
UDP_QUIET_UP_BPS = 3_500_000        # 3.5 Mbit/s


def _load_iperf_settings():
    """Read the night window and UDP interval from the "iperf3" section."""
    try:
        with open(CONFIG_PATH) as f:
            section = json.load(f).get("iperf3", {})
    except FileNotFoundError:
        section = {}
    return (int(section.get("night_start_hour", DEFAULT_NIGHT_START_HOUR)),
            int(section.get("night_end_hour", DEFAULT_NIGHT_END_HOUR)),
            float(section.get("udp_interval_s", DEFAULT_UDP_INTERVAL_S)))


def link_quiet(snmp_job, readings: int, down_bps: float, up_bps: float) -> bool:
    """
    True only if we HAVE the last `readings` SNMP readings and every one of
    them replied, had bps, and was below both lines. Missing data = not quiet.
    """
    recent = snmp_job.recent(readings)
    if len(recent) < readings:
        return False
    return all(r.reachable and r.in_bps is not None and r.out_bps is not None
               and r.in_bps < down_bps and r.out_bps < up_bps
               for r in recent)


class IperfJob:
    """TCP once per night and UDP at most hourly, only when it is safe to test."""

    name = "iperf"

    def __init__(self, state_job, snmp_job,
                 now: Callable[[], datetime] = lambda: datetime.now(SAST),
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.state_job = state_job
        self.snmp_job = snmp_job
        self.now = now              # wall time (for the night window)
        self.clock = clock          # for "how long since" (never jumps)
        self.sleep = sleep          # replaceable, so the test does not wait

        self.server, self.port = iperf.load_server_config()
        self.night_start, self.night_end, self.udp_interval_s = _load_iperf_settings()

        # Checked every round, after state (and traceroute).
        self.interval_s = state_job.interval_s

        self._tcp_done_night: Optional[str] = None   # SAST date of the night TCP worked
        self._last_tcp_try: Optional[float] = None
        self._last_udp_try: Optional[float] = None
        self._plan: List[str] = []                   # tests to run in this round

        self.latest: List[iperf.IperfResult] = []
        self.latest_push_error: Optional[str] = None

    def _since(self, t: Optional[float]) -> float:
        return float("inf") if t is None else self.clock() - t

    def _in_night(self, now: datetime) -> bool:
        return self.night_start <= now.hour < self.night_end

    def should_run(self) -> bool:
        """Decide which tests (if any) are due now."""
        self._plan = []
        if self.state_job.state != state.STATE_UP:
            return False

        now = self.now()
        tonight = now.date().isoformat()

        if (self._in_night(now)
                and self._tcp_done_night != tonight
                and self._since(self._last_tcp_try) >= TCP_RETRY_S
                and link_quiet(self.snmp_job, TCP_QUIET_READINGS,
                               TCP_QUIET_DOWN_BPS, TCP_QUIET_UP_BPS)):
            self._plan += [iperf.TEST_TCP_UPLOAD, iperf.TEST_TCP_DOWNLOAD]

        if (self._since(self._last_udp_try) >= self.udp_interval_s
                and link_quiet(self.snmp_job, UDP_QUIET_READINGS,
                               UDP_QUIET_DOWN_BPS, UDP_QUIET_UP_BPS)):
            self._plan.append(iperf.TEST_UDP)

        return bool(self._plan)

    def _marker(self, test: str, value: int) -> None:
        """Store iperf_test_active{test} = 1 (start) or 0 (end), at this moment."""
        labels = {"device": line_format.DEVICE, "server": self.server, "test": test}
        ts = int(time.time() * 1000)
        victoria.push([line_format.line("iperf_test_active", labels, value, ts)])

    def _one_test(self, test: str) -> iperf.IperfResult:
        self._marker(test, 1)
        if test == iperf.TEST_UDP:
            result = iperf.run_udp(self.server, self.port)
        else:
            result = iperf.run_tcp(self.server, self.port,
                                   download=(test == iperf.TEST_TCP_DOWNLOAD))
        self._marker(test, 0)
        return result

    def run(self) -> List[iperf.IperfResult]:
        """Run the planned tests one after another. Never raises."""
        plan = self._plan or []
        results: List[iperf.IperfResult] = []
        push_error: Optional[str] = None

        if iperf.TEST_TCP_UPLOAD in plan:
            self._last_tcp_try = self.clock()
        if iperf.TEST_UDP in plan:
            self._last_udp_try = self.clock()

        for n, test in enumerate(plan):
            if n > 0:
                self.sleep(iperf.PAUSE_BETWEEN_TESTS_S)    # measured: needed
            result = self._one_test(test)                     # 1. measure
            lines = iperf_lines.to_lines(result)              # 2. translate
            error = victoria.push(lines)                      # 3. send
            push_error = push_error or error
            results.append(result)

        # The TCP night is done only if BOTH TCP tests worked.
        tcp = [r for r in results if r.test != iperf.TEST_UDP]
        if tcp and all(r.success for r in tcp):
            self._tcp_done_night = self.now().date().isoformat()

        self.latest = results                                 # 4. remember
        self.latest_push_error = push_error
        return results


if __name__ == "__main__":
    # Part 1: FAKE time, state, SNMP and iperf3 - shows the decisions only.
    # Nothing is measured and nothing is stored in part 1.
    #   python3 -m src.jobs.iperf_job
    # Part 2 (only with --real): ONE real UDP test to the server in config.json
    # (start `iperf3 -s` on the laptop first). Stored as real data.
    #   python3 -m src.jobs.iperf_job --real
    import sys
    import types

    if "--real" in sys.argv:
        fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)
        quiet = types.SimpleNamespace(reachable=True, in_bps=0.0, out_bps=0.0)
        fake_snmp = types.SimpleNamespace(recent=lambda n: [quiet] * n)
        job = IperfJob(fake_state, fake_snmp)
        job._plan = [iperf.TEST_UDP]
        print(f"One real UDP test to {job.server}:{job.port} ...")
        for r in job.run():
            print(r)
        print("-> pushed" if job.latest_push_error is None
              else f"-> push FAILED: {job.latest_push_error}")
        sys.exit(0)

    victoria.push = lambda lines: None                 # part 1: store nothing
    ran = []

    def fake_test(self, test):
        ran.append(test)
        ok = not (test != iperf.TEST_UDP and fake["tcp_fails"])
        return iperf.IperfResult(timestamp=t["wall"].isoformat(), test=test,
                                 server=self.server, port=self.port,
                                 duration_s=10, success=ok)
    IperfJob._one_test = fake_test

    M = 1_000_000
    fake = {"bps": (0.2 * M, 0.1 * M), "tcp_fails": False}

    def reading():
        return types.SimpleNamespace(reachable=True, in_bps=fake["bps"][0], out_bps=fake["bps"][1])
    fake_snmp = types.SimpleNamespace(recent=lambda n: [reading() for _ in range(n)])
    fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)

    t = {"wall": datetime(2026, 10, 11, 14, 0, tzinfo=SAST), "mono": 0.0}
    job = IperfJob(fake_state, fake_snmp, now=lambda: t["wall"],
                   clock=lambda: t["mono"], sleep=lambda s: None)

    U, D = state.STATE_UP, state.STATE_DOWN
    steps = [
        # (SAST time, minutes since start, state, (in, out) bps, tcp fails?)
        ("14:00", 0,   U, (0.2 * M, 0.1 * M), False),   # day, quiet  -> UDP
        ("14:30", 30,  U, (0.2 * M, 0.1 * M), False),   # < 1 h       -> -
        ("15:01", 61,  U, (8.0 * M, 0.3 * M), False),   # day, busy   -> -
        ("15:02", 62,  U, (0.2 * M, 0.1 * M), False),   # day, quiet  -> UDP
        ("16:05", 125, D, (0.0, 0.0),         False),   # down        -> -
        ("00:10", 610, U, (2.0 * M, 0.1 * M), False),   # night, not quiet enough for TCP -> UDP only
        ("00:40", 640, U, (0.2 * M, 0.1 * M), True),    # night quiet, TCP FAILS -> TCP only
        ("01:41", 701, U, (0.2 * M, 0.1 * M), False),   # retry after 1 h -> TCP + UDP
        ("02:50", 770, U, (0.2 * M, 0.1 * M), False),   # TCP done tonight -> UDP only
        ("05:30", 930, U, (0.2 * M, 0.1 * M), False),   # after window -> UDP only
    ]
    print(f"iperf job: server={job.server}:{job.port}, night {job.night_start:02d}:00-"
          f"{job.night_end:02d}:00 SAST, UDP every {job.udp_interval_s:g} s\n")
    for hhmm, minutes, s, bps, tcp_fails in steps:
        h, m = map(int, hhmm.split(":"))
        day = 11 if minutes < 600 else 12
        t["wall"] = datetime(2026, 10, day, h, m, tzinfo=SAST)
        t["mono"] = minutes * 60.0
        fake_state.state, fake["bps"], fake["tcp_fails"] = s, bps, tcp_fails
        ran.clear()
        if job.should_run():
            job.run()
        what = ", ".join(ran) if ran else "-"
        print(f"  {t['wall']:%d %H:%M} SAST  {state.STATE_NAMES[s]:<14} "
              f"in {bps[0] / M:>4.1f} Mbit/s -> {what}")