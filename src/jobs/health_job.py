"""
src/jobs/health_job.py

The Pi health job. Each time it runs it takes ONE health reading:

    measure (system_health.py) -> translate (health_lines.py) -> send (victoria.py)

and keeps the latest result, so state.py can read it later
(e.g. events E1-E6: low power, overheating, storage, reboot, clock, services).

It does NOT decide WHEN to run - scheduler.py does that, using interval_s.

Why every 10 s, in every state:
  - health exists to explain bad network results ("network bad" vs "Pi
    struggling"), so every network reading needs a health reading from
    about the same time. The tightest network interval is SNMP (10 s),
    so health uses 10 s too, and runs in the same round as SNMP.
  - CPU usage is an average over the interval, so 10 s gives a sharper view.
  - measured cost on the Pi 5: 0.023 s per reading (mean of 10), so at most
    0.23 % of the time at 10 s (elapsed time, not CPU time - real CPU load
    is lower).
  - it only reads local values (/proc, /sys, vcgencmd, systemctl), so it
    does not need the backhaul.

Settings: the "health" section of config.json, e.g.
    "health": {"interval_s": 10}
"""

from typing import Optional

# measure
from src.measurement import system_health
# translate
from src.reporting import health_lines
# send
from src.reporting import victoria
# read interval_s from config.json
from src.jobs.job_config import load_interval_s


# Used when config.json has no "health" -> "interval_s".
DEFAULT_INTERVAL_S = 10.0


class HealthJob:
    """Takes one Pi health reading each time run() is called."""

    name = "health"

    def __init__(self):
        # One monitor for the whole run: CPU usage is worked out from the
        # DIFFERENCE between two readings, so it must remember the last one.
        # (The very first reading therefore has no CPU usage.)
        self.monitor = system_health.SystemHealthMonitor()

        # How often scheduler.py should call run().
        self.interval_s = load_interval_s("health", DEFAULT_INTERVAL_S)

        # The latest reading and push error (None = no error), for state.py.
        self.latest: Optional[system_health.SystemHealthResult] = None
        self.latest_push_error: Optional[str] = None

    def run(self) -> system_health.SystemHealthResult:
        """Take one reading: measure -> translate -> send. Never raises."""

        result = self.monitor.read_once()            # 1. measure
        lines = health_lines.to_lines(result)        # 2. translate
        push_error = victoria.push(lines)            # 3. send

        self.latest = result                         # 4. remember
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: 2 readings, one interval apart (the 2nd has CPU usage).
    import time

    job = HealthJob()
    print(f"Health job: every {job.interval_s:g} s\n")

    for _ in range(2):
        r = job.run()
        print(r)
        print(f"    {len(health_lines.to_lines(r))} lines,",
              "pushed" if job.latest_push_error is None
              else f"push FAILED: {job.latest_push_error}")
        print()
        time.sleep(job.interval_s)