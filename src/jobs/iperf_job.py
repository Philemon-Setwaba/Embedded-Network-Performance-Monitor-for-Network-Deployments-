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

"SERVER BUSY" IS RETRIED AT ONCE (not after 1 hour):
  If a test fails because the server says it is busy, that ONE test is
  tried again after BUSY_WAIT_S (5 s), up to BUSY_TRIES (3) tries in total.
  Seen on 2026-10-12 07:00 SAST: TCP upload and download worked, then UDP
  was refused with "the server is busy running a test. try again later"
  even after the 2 s pause - so the whole set had to wait 1 hour.
  Other failures (e.g. no reply from the server: each one already took
  25 s) are NOT retried at once: the server is probably off, so trying
  again straight away would only keep the active lane busy (ping waits).
  5 s and 3 tries are PROJECT CHOICES (at most +10 s per test).
  Only the LAST try of a test is stored; iperf_attempts{test} says how
  many tries it took (1 = first time).

REMEMBERED ACROSS RESTARTS: which slots are done and when each slot was
last tried are saved in data/iperf_state.json (next to config.json), e.g.
    {"done": ["2026-10-11 afternoon"],
     "last_try": {"2026-10-11 afternoon": 1791719852.0}}
so a restart, crash, reboot or power cut does NOT cause an extra set
(seen on 2026-10-11: two scheduler starts in the afternoon = two sets).
Only the last KEEP_DAYS days are kept, so the file stays tiny.
If the file is missing or broken, the job starts empty (never crashes).
Times in the file are wall-clock seconds (time.time()), because the
monotonic clock restarts at every boot.

Why: the night set gives a clean capacity number (link quiet); the three
day sets show how the link behaves when people use it. The slots, the
link checks and the 1 h retry are PROJECT CHOICES.

Cost: one set on the iNethi link ~ 18.6 MB (TCP 10 s x 10 Mbit/s down
+ 10 s x 5 Mbit/s up ~ 18.75 MB, + UDP ~0.6 MB); 4 sets/day ~ 75 MB/day.
While a set runs (~35 s measured), the active lane is busy and ping waits
(measured: 3 ping checks skipped).

Own traffic marker: right before and right after each try it stores
    iperf_test_active{test, slot} 1   (start)
    iperf_test_active{test, slot} 0   (end)
so Grafana can leave the monitor's OWN iperf3 traffic out of "link use".

Settings: the "iperf3" section of config.json: "server", "port".
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

# "Server busy": retry that one test at once (see top of file).
BUSY_TEXT = "busy"      # part of iperf3's message "the server is busy running a test..."
BUSY_WAIT_S = 5.0       # wait before trying again (project choice)
BUSY_TRIES = 3          # tries in total for one test (project choice)

# The order of the three tests in one set.
SET_TESTS = [iperf.TEST_TCP_UPLOAD, iperf.TEST_TCP_DOWNLOAD, iperf.TEST_UDP]

# Where "done" and "last try" are saved, and how many days to keep.
STATE_PATH = CONFIG_PATH.parent / "data" / "iperf_state.json"
KEEP_DAYS = 3


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


def server_busy(result: "iperf.IperfResult") -> bool:
    """True if the test failed because the server said it is busy."""
    return (not result.success
            and BUSY_TEXT in (result.error_detail or "").lower())


class IperfJob:
    """One set (TCP up, TCP down, UDP) per slot per day, only when safe."""

    name = "iperf"

    def __init__(self, state_job, snmp_job,
                 now: Callable[[], datetime] = lambda: datetime.now(SAST),
                 sleep: Callable[[float], None] = time.sleep,
                 state_path: Path = STATE_PATH):
        self.state_job = state_job
        self.snmp_job = snmp_job
        self.now = now              # wall time (slots AND "1 h since last try")
        self.sleep = sleep          # replaceable, so the test does not wait
        self.state_path = Path(state_path)

        self.server, self.port = iperf.load_server_config()

        # Checked every round, after state (and traceroute).
        self.interval_s = state_job.interval_s

        # Remembered across restarts (see top of file).
        self._done, self._last_try = self._load_state()
        self._slot_key: Optional[str] = None     # the slot this run is for
        self._slot_name: Optional[str] = None

        self.latest: List[iperf.IperfResult] = []
        self.latest_push_error: Optional[str] = None

    # -----------------------------------------------------------------------
    # The saved file
    # -----------------------------------------------------------------------

    def _load_state(self):
        """Read done slots and last tries from the file; empty if missing/broken."""
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            done = set(str(k) for k in data.get("done", []))
            last_try = {str(k): float(v) for k, v in data.get("last_try", {}).items()}
            return done, last_try
        except (OSError, ValueError, TypeError, AttributeError):
            return set(), {}

    def _save_state(self) -> None:
        """Write the file, keeping only the last KEEP_DAYS days. Never raises."""
        oldest = (self.now().date() - timedelta(days=KEEP_DAYS - 1)).isoformat()
        keep = lambda key: key[:10] >= oldest          # key = "YYYY-MM-DD slot"
        self._done = {k for k in self._done if keep(k)}
        self._last_try = {k: v for k, v in self._last_try.items() if keep(k)}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump({"done": sorted(self._done), "last_try": self._last_try},
                          f, indent=1)
            os.replace(tmp, self.state_path)           # all-or-nothing write
        except OSError:
            pass   # cannot save: the job still works, it just forgets on restart

    # -----------------------------------------------------------------------
    # When to run
    # -----------------------------------------------------------------------

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
        if last is not None and now.timestamp() - last < RETRY_S:
            return False
        if not link_quiet(self.snmp_job, readings, down_bps, up_bps):
            return False

        self._slot_key, self._slot_name = key, name
        return True

    # -----------------------------------------------------------------------
    # One set
    # -----------------------------------------------------------------------

    def _marker(self, test: str, value: int) -> None:
        """Store iperf_test_active{test, slot} = 1 (start) or 0 (end), at this moment."""
        labels = {"device": line_format.DEVICE, "server": self.server,
                  "test": test, "slot": self._slot_name or ""}
        ts = int(time.time() * 1000)
        victoria.push([line_format.line("iperf_test_active", labels, value, ts)])

    def _one_test(self, test: str) -> iperf.IperfResult:
        """One try of one test, with the start/end markers."""
        self._marker(test, 1)
        if test == iperf.TEST_UDP:
            result = iperf.run_udp(self.server, self.port)
        else:
            result = iperf.run_tcp(self.server, self.port,
                                   download=(test == iperf.TEST_TCP_DOWNLOAD))
        self._marker(test, 0)
        return result

    def _test_with_busy_retry(self, test: str):
        """
        Run one test. If the server says it is busy, wait BUSY_WAIT_S and try
        again, up to BUSY_TRIES tries. Returns (last result, number of tries).
        """
        tries = 1
        result = self._one_test(test)
        while server_busy(result) and tries < BUSY_TRIES:
            self.sleep(BUSY_WAIT_S)
            tries += 1
            result = self._one_test(test)
        return result, tries

    def run(self) -> List[iperf.IperfResult]:
        """Run the whole set: TCP up, pause, TCP down, pause, UDP. Never raises."""
        key = self._slot_key
        if key is not None:
            self._last_try[key] = self.now().timestamp()
            self._save_state()            # saved BEFORE the set: a crash mid-set
                                          # still counts as a try (1 h retry)

        results: List[iperf.IperfResult] = []
        push_error: Optional[str] = None
        for n, test in enumerate(SET_TESTS):
            if n > 0:
                self.sleep(iperf.PAUSE_BETWEEN_TESTS_S)    # measured: needed
            result, tries = self._test_with_busy_retry(test)   # 1. measure
            lines = iperf_lines.to_lines(result)              # 2. translate
            lines.append(line_format.line(                    #    + how many tries
                "iperf_attempts",
                {"device": line_format.DEVICE, "server": result.server,
                 "port": str(result.port), "test": result.test},
                tries, line_format.to_ms(result.timestamp)))
            error = victoria.push(lines)                      # 3. send
            push_error = push_error or error
            results.append(result)

        # The slot is done only if ALL three tests worked.
        if key is not None and all(r.success for r in results):
            self._done.add(key)
            self._save_state()

        self.latest = results                                 # 4. remember
        self.latest_push_error = push_error
        return results


if __name__ == "__main__":
    # Part 1: FAKE time, state, SNMP and iperf3, and a TEMPORARY state file -
    # shows the decisions, including a restart and a "server busy" retry.
    # Nothing is measured, nothing is stored in VictoriaMetrics, and the
    # real data/iperf_state.json is not touched.
    #   python3 -m src.jobs.iperf_job
    # Part 2 (only with --real): ONE real set (TCP up, TCP down, UDP) to the
    # server in config.json (start `iperf3 -s` on the laptop first).
    # Stored as real data. Uses (and updates) the real state file.
    #   python3 -m src.jobs.iperf_job --real
    import sys
    import tempfile
    import types

    if "--real" in sys.argv:
        fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)
        quiet = types.SimpleNamespace(reachable=True, in_bps=0.0, out_bps=0.0)
        fake_snmp = types.SimpleNamespace(recent=lambda n: [quiet] * n)
        job = IperfJob(fake_state, fake_snmp)
        if not job.should_run():
            print(f"Not run: this slot is already done or was tried < 1 h ago "
                  f"(see {job.state_path}).")
            sys.exit(0)
        print(f"One real set to {job.server}:{job.port} (slot {job._slot_name}) ...")
        start = time.monotonic()
        for r in job.run():
            print(r)
        print(f"set took {time.monotonic() - start:.1f} s")
        print("-> pushed" if job.latest_push_error is None
              else f"-> push FAILED: {job.latest_push_error}")
        print(f"saved: {job.state_path}")
        sys.exit(0)

    victoria.push = lambda lines: None                 # part 1: store nothing
    ran = []
    BUSY_MSG = "the server is busy running a test. try again later"

    def fake_test(self, test):
        """Fake one try. fake["busy"] = how many UDP tries get 'server busy'."""
        ran.append(test)
        if test == iperf.TEST_UDP and fake["busy"] > 0:
            fake["busy"] -= 1
            return iperf.IperfResult(timestamp=t["wall"].isoformat(), test=test,
                                     server=self.server, port=self.port,
                                     duration_s=10, success=False,
                                     error_detail=BUSY_MSG)
        ok = not fake["fail"]
        return iperf.IperfResult(timestamp=t["wall"].isoformat(), test=test,
                                 server=self.server, port=self.port,
                                 duration_s=10, success=ok)
    IperfJob._one_test = fake_test

    M = 1_000_000
    fake = {"bps": (0.2 * M, 0.1 * M), "fail": False, "busy": 0}

    def reading():
        return types.SimpleNamespace(reachable=True, in_bps=fake["bps"][0], out_bps=fake["bps"][1])
    fake_snmp = types.SimpleNamespace(recent=lambda n: [reading() for _ in range(n)])
    fake_state = types.SimpleNamespace(state=state.STATE_UP, interval_s=10.0)

    tmp_file = Path(tempfile.mkdtemp()) / "iperf_state.json"
    t = {"wall": None}

    def new_job():
        return IperfJob(fake_state, fake_snmp, now=lambda: t["wall"],
                        sleep=lambda s: None, state_path=tmp_file)
    job = new_job()

    U, D = state.STATE_UP, state.STATE_DOWN
    RESTART = "RESTART"
    steps = [
        # (day, SAST time, state, (in, out) bps, set fails?, UDP busy tries) or RESTART
        (12, "00:10", U, (2.0 * M, 0.1 * M), False, 0),   # night, not quiet -> -
        (12, "00:20", U, (0.2 * M, 0.1 * M), True,  0),   # night, set FAILS (no reply) -> set
        RESTART,
        (12, "00:50", U, (0.2 * M, 0.1 * M), False, 0),   # < 1 h after the failure -> -
        (12, "01:21", U, (0.2 * M, 0.1 * M), False, 0),   # retry -> set (night done)
        RESTART,
        (12, "03:00", U, (0.2 * M, 0.1 * M), False, 0),   # night done -> -
        (12, "05:30", U, (0.2 * M, 0.1 * M), False, 0),   # no slot -> -
        (12, "06:05", D, (0.0, 0.0),         False, 0),   # morning, backhaul down -> -
        (12, "06:15", U, (8.0 * M, 0.3 * M), False, 0),   # morning, busy link -> -
        (12, "06:16", U, (2.0 * M, 0.3 * M), False, 1),   # UDP "server busy" once -> retried, done
        RESTART,
        (12, "09:00", U, (0.2 * M, 0.1 * M), False, 0),   # morning done -> -
        (12, "12:00", U, (5.0 * M, 1.0 * M), False, 3),   # UDP busy 3 times -> gives up, NOT done
        (12, "12:30", U, (0.2 * M, 0.1 * M), False, 0),   # < 1 h -> -
        (12, "13:01", U, (0.2 * M, 0.1 * M), False, 0),   # 1 h later -> set (afternoon done)
        (12, "18:30", U, (0.2 * M, 0.1 * M), False, 0),   # evening -> set
        (13, "00:01", U, (0.2 * M, 0.1 * M), False, 0),   # next night -> set
    ]
    print(f"iperf job: server={job.server}:{job.port}, slots: "
          + ", ".join(f"{n} {a:02d}-{b:02d}" for n, a, b, _ in SLOTS)
          + f" SAST; 'server busy' -> wait {BUSY_WAIT_S:g} s, up to {BUSY_TRIES} tries\n")
    for step in steps:
        if step == RESTART:
            job = new_job()
            print("  -- restart: new job, reads the saved file --")
            continue
        day, hhmm, s, bps, fail, busy = step
        h, m = map(int, hhmm.split(":"))
        t["wall"] = datetime(2026, 10, day, h, m, tzinfo=SAST)
        fake_state.state, fake["bps"], fake["fail"], fake["busy"] = s, bps, fail, busy
        ran.clear()
        if job.should_run():
            job.run()
        slot = current_slot(t["wall"])
        if not ran:
            what = "-"
        else:
            udp_tries = ran.count(iperf.TEST_UDP)
            ok = all(r.success for r in job.latest)
            what = f"SET ({'ok' if ok else 'failed'}), UDP tries: {udp_tries}"
        print(f"  {t['wall']:%d %H:%M} {slot[0] if slot else '(none)':<9} "
              f"{state.STATE_NAMES[s]:<14} in {bps[0] / M:>4.1f} Mbit/s -> {what}")

    print(f"\nsaved file at the end (temporary, {tmp_file}):")
    print(tmp_file.read_text())