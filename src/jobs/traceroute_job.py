"""
src/jobs/traceroute_job.py

The traceroute job. When it runs it does ONE traceroute:

    measure (traceroute.py) -> translate (traceroute_lines.py) -> send (victoria.py)

Ping shows THAT the backhaul is down; traceroute shows WHERE along the path
replies stop. It does not measure how full a link is (Steenbergen, NANOG 47:
routers answer traceroute at low priority, so per-hop times are only a hint).

When it runs - NOT on a plain timer. It sits in the active lane right after
the state job and, each round, should_run() checks the backhaul state:

  reason       when                                              why
  down         state just changed to "backhaul down"             where does it break?
  still_down   still down, and DOWN_REPEAT_S (30 min) since       did the break point move?
               the last run
  back_up      state just changed from "down" back to "up"       path right after the repair
                                                                  (may differ if rerouted)
  baseline     up, and BASELINE_S (1 hour) since the last run    the NORMAL path, to compare
               (also the first run after start-up)               a broken path against
  (no run)     "local link down" or "unknown"                    the Pi itself has no network

The reason is stored as traceroute_run_info{reason="..."} 1, so Grafana
can show why each run happened.

Why 30 min while down (project choice): the repeats only show whether the
break point moves. In a long outage (hours or days) a shorter repeat mostly
stores the same "* * *" again and blocks ping more often. One run blocks
the active lane (so ping waits) for at most ~12 s (measured, -w 2, all
probes lost).

Why 1 hour while up (project choice): the normal path changes (hops seen
changing between runs on 2026-10-11), so the baseline should be recent.
Cost: at most 90 probes x 60 bytes = 5.4 kB sent per run, ~0.13 MB/day.

Settings: the "traceroute" section of config.json, e.g.
    "traceroute": {"target": "8.8.8.8", "down_repeat_s": 1800, "baseline_s": 3600}
"""

import json
import time
from typing import Callable, Optional

# measure
from src.measurement import traceroute
# translate
from src.reporting import traceroute_lines, line_format
# send
from src.reporting import victoria
# backhaul state codes, config.json location
from src.jobs import state
from src.jobs.job_config import CONFIG_PATH


# Used when config.json has no "traceroute" section (or no setting in it).
DEFAULT_TARGET = traceroute.DEFAULT_TARGET     # "8.8.8.8", same as ping
DEFAULT_DOWN_REPEAT_S = 1800.0                 # 30 min (project choice)
DEFAULT_BASELINE_S = 3600.0                    # 1 hour (project choice)

REASON_DOWN = "down"
REASON_STILL_DOWN = "still_down"
REASON_BACK_UP = "back_up"
REASON_BASELINE = "baseline"


def _load_traceroute_settings():
    """Read "target", "down_repeat_s" and "baseline_s" from config.json."""
    try:
        with open(CONFIG_PATH) as f:
            section = json.load(f).get("traceroute", {})
    except FileNotFoundError:
        section = {}
    return (section.get("target", DEFAULT_TARGET),
            float(section.get("down_repeat_s", DEFAULT_DOWN_REPEAT_S)),
            float(section.get("baseline_s", DEFAULT_BASELINE_S)))


class TracerouteJob:
    """One traceroute, only when the backhaul state calls for it."""

    name = "traceroute"

    def __init__(self, state_job, clock: Callable[[], float] = time.monotonic):
        self.state_job = state_job
        self.clock = clock          # replaceable, so the test can fake the time

        self.target, self.down_repeat_s, self.baseline_s = _load_traceroute_settings()

        # Checked every round, right after the state job.
        self.interval_s = state_job.interval_s

        self._last_state = state.STATE_UNKNOWN   # backhaul state seen last round
        self._last_run: Optional[float] = None   # clock time of the last run
        self._reason: Optional[str] = None       # why the next run happens

        # The latest result and push error (None = no error).
        self.latest: Optional[traceroute.TracerouteResult] = None
        self.latest_reason: Optional[str] = None
        self.latest_push_error: Optional[str] = None

    def _since_last_run(self) -> float:
        """Seconds since the last run (infinite if it never ran)."""
        return float("inf") if self._last_run is None else self.clock() - self._last_run

    def should_run(self) -> bool:
        """Decide from the backhaul state whether to run now, and why."""
        now_state = self.state_job.state
        before = self._last_state
        self._last_state = now_state

        if now_state == state.STATE_DOWN:
            if before != state.STATE_DOWN:
                self._reason = REASON_DOWN
            elif self._since_last_run() >= self.down_repeat_s:
                self._reason = REASON_STILL_DOWN
            else:
                return False
            return True

        if now_state == state.STATE_UP:
            if before == state.STATE_DOWN:
                self._reason = REASON_BACK_UP
            elif self._since_last_run() >= self.baseline_s:
                self._reason = REASON_BASELINE
            else:
                return False
            return True

        # local link down or unknown: no network to trace.
        return False

    def run(self) -> traceroute.TracerouteResult:
        """Do one traceroute: measure -> translate -> send. Never raises."""
        reason = self._reason or REASON_BASELINE
        self._last_run = self.clock()

        result = traceroute.run_traceroute(self.target)          # 1. measure
        lines = traceroute_lines.to_lines(result)                # 2. translate
        lines.append(line_format.line(                           #    + why it ran
            "traceroute_run_info",
            {"device": line_format.DEVICE, "target": result.target, "reason": reason},
            1, line_format.to_ms(result.timestamp)))
        push_error = victoria.push(lines)                        # 3. send

        self.latest = result                                     # 4. remember
        self.latest_reason = reason
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test, part 1: FAKE states and a FAKE clock - shows when it would
    # run and why. Nothing is traced and nothing is sent in part 1.
    import types

    fake_state = types.SimpleNamespace(state=state.STATE_UNKNOWN, interval_s=10.0)
    fake_time = [0.0]
    job = TracerouteJob(fake_state, clock=lambda: fake_time[0])
    print(f"Traceroute job: target={job.target}, repeat while down every "
          f"{job.down_repeat_s:g} s, baseline every {job.baseline_s:g} s\n")

    U, D, L, X = state.STATE_UP, state.STATE_DOWN, state.STATE_LOCAL_DOWN, state.STATE_UNKNOWN
    steps = [
        # (minutes since start, backhaul state)
        (0, X), (0.2, U), (10, U), (61, U),
        (70, D), (71, D), (99, D), (101, D),
        (105, U), (106, U), (120, L), (130, U), (166, U),
    ]
    names = dict(state.STATE_NAMES)
    for minutes, s in steps:
        fake_time[0] = minutes * 60
        fake_state.state = s
        if job.should_run():
            job._last_run = job.clock()          # pretend it ran (no real traceroute)
            what = f"RUN ({job._reason})"
        else:
            what = "-"
        print(f"  t={minutes:>5g} min  {names[s]:<16} -> {what}")

    # Part 2: ONE real traceroute, stored with reason "baseline".
    print("\nOne real traceroute (reason baseline):")
    real = TracerouteJob(types.SimpleNamespace(state=U, interval_s=10.0))
    real.should_run()
    r = real.run()
    print(f"  status={r.status}, hops={len(r.hops)}, reason={real.latest_reason}")
    print("  -> pushed" if real.latest_push_error is None
          else f"  -> push FAILED: {real.latest_push_error}")