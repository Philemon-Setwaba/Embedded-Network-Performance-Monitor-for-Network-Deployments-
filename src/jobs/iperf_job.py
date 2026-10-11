"""
src/jobs/iperf_job.py

The iperf3 job: ONE SET of three tests, always together:
    1. TCP upload        (Pi sends)          -> throughput, retransmits
    2. TCP download (-R) (server sends)      -> throughput
    3. UDP 500 kbit/s                        -> jitter, loss
with a 2 s pause between tests (iperf.PAUSE_BETWEEN_TESTS_S: measured - a
test started right after another was refused by the server).

    measure (iperf.py) -> translate (iperf_lines.py) -> send (victoria.py)

WHEN - one set per SLOT per day (times are South Africa time, SAST = UTC+2):

  slot        time (SAST)     link check before the set
  night       00:00 - 05:00   QUIET: last 5 min (30 SNMP readings) all
                              < 1 Mbit/s down and < 0.5 Mbit/s up
  morning     06:00 - 12:00   NOT BUSY: last 1 min (6 SNMP readings) all
  afternoon   12:00 - 18:00   < 7 Mbit/s down and < 3.5 Mbit/s up
  evening     18:00 - 24:00   (70 % of the iNethi 10 / 5 Mbit/s link)
  (05:00 - 06:00: no slot)

  - never while the backhaul is down / local link down / unknown
  - a slot is DONE when all 3 tests worked; if any failed (e.g. server
    not reachable), the set is tried again after 1 hour, inside the slot
  - if the link check never passes in a slot, that slot has no set

Why: the night set gives a clean capacity number (link quiet); the three
day sets show how the link behaves when people use it. The slots, the
link checks and the 1 h retry are PROJECT CHOICES.

Cost: one set on the iNethi link ~ 18.6 MB (TCP 10 s x 10 Mbit/s down
+ 10 s x 5 Mbit/s up ~ 18.75 MB, + UDP ~0.6 MB); 4 sets/day ~ 75 MB/day.
While a set runs (~36 s + iperf3 start-up), the active lane is busy and
ping waits (measured: one ping check is skipped).

Own traffic marker: right before and right after each test it stores
    iperf_test_active{test, slot} 1   (start)
    iperf_test_active{test, slot} 0   (end)
so Grafana can leave the monitor's OWN iperf3 traffic out of "link use".

Settings: the "iperf3" section of config.json: "server", "port".
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

# measure
from src.measurement import iperf
# translate
from src.reporting import iperf_lines, line_format
# send
from src.reporting import victoria
# backhaul state codes
from src.jobs import state


# South Africa Standard Time = UTC+2 all year (South Africa has no daylight saving).
SAST = timezone(timedelta(hours=2), "SAST")

# Link checks: (readings, down line bps, up line bps)
CHECK_QUIET = (30, 1_000_000, 500_000)        # 5 min below 1 / 0.5 Mbit/s
CHECK_NOT_BUSY = (6, 7_000_000, 3_500_000)    # 1 min below 7 / 3.5 Mbit/s

# Slots: (name, start hour, end hour (not included), link check)
SLOTS = [
    ("night",     0,  5, CHECK_QUIET),
    ("morning",   6, 12, CHECK_NOT_BUSY),
    ("afternoon", 12, 18, CHECK_NOT_BUSY),
    ("evening",   18, 24, CHECK_NOT_BUSY),
]

RETRY_S = 3600.0        # a failed set: try again after 1 h (inside the slot)

# The order of the three tests in one set.
SET_TESTS = [iperf.TEST_TCP_UPLOAD, iperf.TEST_TCP_DOWNLOAD, iperf.TEST_UDP]


def current_slot(now: datetime):
    """The slot (name, start, end, check) for this time, or None (05:00-06:00)."""
    for slot in SLOTS:
        if slot[1] <= now.hour < slot[2]:
            return slot
    return None


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
    """One set (TCP up, TCP down, UDP) per slot per day, only when safe."""

    name = "iperf"

    def __init__(self, state_job, snmp_job,
                 now: Callable[[], datetime] = lambda: datetime.now(SAST),
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.state_job = state_job
        self.snmp_job = snmp_job
        self.now = now              # wall time (for the slots)
        self.clock = clock          # for "how long since" (never jumps)
        self.sleep = sleep          # replaceable, so the test does not wait

        self.server, self.port = iperf.load_server_config()

        # Checked every round, after state (and traceroute).
        self.interval_s = state_job.interval_s

        self._done = set()                       # {"2026-10-11 morning", ...}
        self._last_try = {}                      # slot key -> clock time of last try
        self._slot_key: Optional[str] = None     # the slot this run is for
        self._slot_name: Optional[str] = None

        self.latest: List[iperf.IperfResult] = []
        self.latest_push_error: Optional[str] = None

    def should_run(self) -> bool:
        """True if this slot has no set yet, it is safe, and not tried < 1 h ago."""
        if self.state_job.state != state.STATE_UP:
            return False

        now = self.now()
        slot = current_slot(now)
        if slot is None:
            return False
        name, _, _, (readings, down_bps, up_bps) = slot
        key = f"{now.date().isoformat()} {name}"

        if key in self._done:
            return False
        last = self._last_try.get(key)
        if last is not None and self.clock() - last < RETRY_S:
            return False
        if not link_quiet(self.snmp_job, readings, down_bps, up_bps):
            return False

        self._slot_key, self._slot_name = key, name
        return True

    def _marker(self, test: str, value: int) -> None:
        """Store iperf_test_active{test, slot} = 1 (start) or 0 (end), at this moment."""
        labels = {"device": line_format.DEVICE, "server": self.server,
                  "test": test, "slot": self._slot_name or ""}
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
        """Run the whole set: TCP up, pause, TCP down, pause, UDP. Never raises."""
        key = self._slot_key
        self._last_try[key] = self.clock()

        results: List[iperf.IperfResult] = []
        push_error: Optional[str] = None
        for n, test in enumerate(SET_TESTS):
            if n > 0:
                self.sleep(iperf.PAUSE_BETWEEN_TESTS_S)    # measured: needed
            result = self._one_test(test)                     # 1. measure
            lines = iperf_lines.to_lines(result)              # 2. translate
            error = victoria.push(lines)                      # 3. send
            push_error = push_error or error
            results.append(result)

        # The slot is done only if ALL three tests worked.
        if all(r.success for r in results):
            self._done.add(key)

        self.latest = results                                 # 4. remember
        self.latest_push_error = push_error
        return results


if __name__ == "__main__":
    # Part 1: FAKE time, state, SNMP and iperf3 - shows the decisions only.
    # Nothing is measured and nothing is stored in part 1.
    #   python3 -m src.jobs.iperf_job
    # Part 2 (only with --real): ONE real set (TCP up, TCP down, UDP) to the
    # server in config.json (start `iperf3 -s` on the laptop first).
    # Stored as real data.
    #   python3 -m src.jobs.iperf_job --real
    import sys
    import types

    if "--real" in sys.argv:
        fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)
        quiet = types.SimpleNamespace(reachable=True, in_bps=0.0, out_bps=0.0)
        fake_snmp = types.SimpleNamespace(recent=lambda n: [quiet] * n)
        job = IperfJob(fake_state, fake_snmp)
        job.should_run()
        print(f"One real set to {job.server}:{job.port} (slot {job._slot_name}) ...")
        start = time.monotonic()
        for r in job.run():
            print(r)
        print(f"set took {time.monotonic() - start:.1f} s")
        print("-> pushed" if job.latest_push_error is None
              else f"-> push FAILED: {job.latest_push_error}")
        sys.exit(0)

    victoria.push = lambda lines: None                 # part 1: store nothing
    ran = []

    def fake_test(self, test):
        ran.append(test)
        ok = not fake["fail"]
        return iperf.IperfResult(timestamp=t["wall"].isoformat(), test=test,
                                 server=self.server, port=self.port,
                                 duration_s=10, success=ok)
    IperfJob._one_test = fake_test

    M = 1_000_000
    fake = {"bps": (0.2 * M, 0.1 * M), "fail": False}

    def reading():
        return types.SimpleNamespace(reachable=True, in_bps=fake["bps"][0], out_bps=fake["bps"][1])
    fake_snmp = types.SimpleNamespace(recent=lambda n: [reading() for _ in range(n)])
    fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)

    t = {"wall": None, "mono": 0.0}
    job = IperfJob(fake_state, fake_snmp, now=lambda: t["wall"],
                   clock=lambda: t["mono"], sleep=lambda s: None)

    U, D = state.STATE_UP, state.STATE_DOWN
    steps = [
        # (day, SAST time, state, (in, out) bps, set fails?)
        (12, "00:10", U, (2.0 * M, 0.1 * M), False),   # night, not quiet (2 Mbit/s) -> -
        (12, "00:20", U, (0.2 * M, 0.1 * M), True),    # night, quiet, set FAILS -> set
        (12, "00:50", U, (0.2 * M, 0.1 * M), False),   # < 1 h after the failure -> -
        (12, "01:21", U, (0.2 * M, 0.1 * M), False),   # retry -> set (night done)
        (12, "03:00", U, (0.2 * M, 0.1 * M), False),   # night done -> -
        (12, "05:30", U, (0.2 * M, 0.1 * M), False),   # no slot -> -
        (12, "06:05", D, (0.0, 0.0),         False),   # morning, backhaul down -> -
        (12, "06:15", U, (8.0 * M, 0.3 * M), False),   # morning, busy -> -
        (12, "06:16", U, (2.0 * M, 0.3 * M), False),   # morning, not busy -> set
        (12, "09:00", U, (0.2 * M, 0.1 * M), False),   # morning done -> -
        (12, "12:00", U, (5.0 * M, 1.0 * M), False),   # afternoon, not busy -> set
        (12, "18:30", U, (0.2 * M, 0.1 * M), False),   # evening -> set
        (12, "23:59", U, (0.2 * M, 0.1 * M), False),   # evening done -> -
        (13, "00:01", U, (0.2 * M, 0.1 * M), False),   # next night -> set
    ]
    print(f"iperf job: server={job.server}:{job.port}, slots: "
          + ", ".join(f"{n} {a:02d}-{b:02d}" for n, a, b, _ in SLOTS) + " SAST\n")
    for day, hhmm, s, bps, fail in steps:
        h, m = map(int, hhmm.split(":"))
        t["wall"] = datetime(2026, 10, day, h, m, tzinfo=SAST)
        t["mono"] = ((day - 12) * 24 * 60 + h * 60 + m) * 60.0
        fake_state.state, fake["bps"], fake["fail"] = s, bps, fail
        ran.clear()
        if job.should_run():
            job.run()
        slot = current_slot(t["wall"])
        what = ("SET " + ("(failed)" if fail else "(ok)")) if ran else "-"
        print(f"  {t['wall']:%d %H:%M} {slot[0] if slot else '(none)':<9} "
              f"{state.STATE_NAMES[s]:<14} in {bps[0] / M:>4.1f} Mbit/s -> {what}")