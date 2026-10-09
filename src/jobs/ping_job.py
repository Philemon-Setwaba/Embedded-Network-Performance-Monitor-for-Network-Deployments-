"""
src/jobs/ping_job.py

The ping job. Each time it runs it does ONE ping measurement:

    measure (ping.py) -> translate (ping_lines.py) -> send (victoria.py)

and keeps the latest result, so state.py can read it later
(events B1 backhaul down, B3 high latency, B4 latency variation, B5 loss).

It does NOT decide WHEN to run - scheduler.py does that, using interval_s.
Ping is an ACTIVE test (it sends packets over the link), so later it runs
in the active lane: never at the same time as TCP, traceroute or iperf3.

Why 5 packets:
  - loss in steps of 20 %; finer loss is judged over a WINDOW of checks
    (e.g. last 10 checks = 50 packets = 2 % steps) in state.py, so each
    check stays short.
  - measured on the Pi: 5 packets take ~4 s with replies, ~6.1 s with no
    reply (4.1 s + 2 s wait for the last reply, -W 2).

Why every 10 s:
  - "backhaul down" = 2 failed checks in a row, so an outage is noticed in
    about 20-24 s (at 30 s it would be ~60 s).
  - same 10 s grid as SNMP and health, so readings line up in time.
  - cost with iNethi as reference (5 Mbit/s uplink): 10 packets x 84 bytes
    per check ~ 0.013 % of the uplink, ~7.3 MB per day.
  - longest check (no reply, 6.1 s) is still shorter than 10 s.
Limit: no data on how long real iNethi outages last, so 10 s is a reasoned
choice, not one measured from outages.

Settings: the "ping" section of config.json, e.g.
    "ping": {"target": "8.8.8.8", "count": 5, "interval_s": 10}
"""

import json
from typing import Optional

# measure
from src.measurement import ping
# translate
from src.reporting import ping_lines
# send
from src.reporting import victoria
# config.json location and interval reader
from src.jobs.job_config import CONFIG_PATH, load_interval_s


# Used when config.json has no "ping" section (or no setting in it).
DEFAULT_INTERVAL_S = 10.0
DEFAULT_TARGET = "8.8.8.8"
DEFAULT_COUNT = 5


def _load_ping_settings():
    """Read "target" and "count" from the "ping" section of config.json."""
    try:
        with open(CONFIG_PATH) as f:
            section = json.load(f).get("ping", {})
    except FileNotFoundError:
        section = {}
    return section.get("target", DEFAULT_TARGET), int(section.get("count", DEFAULT_COUNT))


class PingJob:
    """Does one ping measurement each time run() is called."""

    name = "ping"

    def __init__(self):
        # Who to ping and how many packets, from config.json.
        target, self.count = _load_ping_settings()
        self.measurer = ping.PingMeasurer(target=target, count=self.count)

        # How often scheduler.py should call run().
        self.interval_s = load_interval_s("ping", DEFAULT_INTERVAL_S)

        # The latest result and push error (None = no error), for state.py.
        self.latest: Optional[ping.PingResult] = None
        self.latest_push_error: Optional[str] = None

    def run(self) -> ping.PingResult:
        """Do one measurement: measure -> translate -> send. Never raises."""

        result = self.measurer.measure_once()             # 1. measure
        lines = ping_lines.to_lines(result, self.count)   # 2. translate
        push_error = victoria.push(lines)                 # 3. send

        self.latest = result                              # 4. remember
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: 2 measurements, one interval apart, printing what was sent.
    import time

    job = PingJob()
    print(f"Ping job: target={job.measurer.target}, count={job.count}, "
          f"every {job.interval_s:g} s\n")

    for _ in range(2):
        start = time.monotonic()
        r = job.run()
        print(r)
        for line in ping_lines.to_lines(r, job.count):
            print("   ", line)
        print("    -> pushed" if job.latest_push_error is None
              else f"    -> push FAILED: {job.latest_push_error}")
        print()
        time.sleep(max(0.0, job.interval_s - (time.monotonic() - start)))