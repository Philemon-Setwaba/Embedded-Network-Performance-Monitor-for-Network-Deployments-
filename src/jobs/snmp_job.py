"""
src/jobs/snmp_job.py

The SNMP job. Each time it runs it does ONE poll:

    measure  (snmp.py)  ->  translate (snmp_lines.py)  ->  send (victoria.py)

and keeps:
  - latest   the newest poll result
  - history  the last HISTORY_READINGS poll results (oldest first)
so state.py can read them later.

Why a history:
  - "link full" needs the last 6 readings (1 minute at 10 s) to all be
    above the line, not just one reading
  - later, "is the link quiet before iperf3?" needs the last 5 minutes
  30 readings x 10 s = 5 minutes covers both.
The history is only kept in memory (it is lost on restart). The data
itself is still stored in VictoriaMetrics as before - nothing changed there.

It does NOT decide WHEN to run - scheduler.py does that, using interval_s.

Why SNMP runs every 10 s, in every state (also when the backhaul is down):
  - it only reads counters on the router on the local network, so it
    does not need the backhaul;
  - it shows what happens to traffic during an outage, and when it starts/ends;
  - the readings are stored on the Pi, so there is no gap in the record;
  - the "link full" and "quiet before iperf3" checks need recent readings.
The interval must be longer than the time the agent takes to update its
counters (a few seconds), or two readings can show the same value.

Settings: the "snmp" section of config.json, e.g.
    "snmp": {"target": "192.168.1.1", "interface": "lte0", "community": "public",
             "interval_s": 10}
"""

from collections import deque
from typing import List, Optional

# measure
from src.measurement import snmp
# translate
from src.reporting import snmp_lines
# send
from src.reporting import victoria
# read interval_s from config.json
from src.jobs.job_config import load_interval_s


# Used when config.json has no "interval_s" in its "snmp" section.
DEFAULT_INTERVAL_S = 10.0

# How many past readings to keep: 30 x 10 s = 5 minutes.
HISTORY_READINGS = 30


class SnmpJob:
    """Polls SNMP once each time run() is called."""

    name = "snmp"

    def __init__(self):
        # Target, interface name and community from config.json (snmp.py reads them).
        target, if_name, community = snmp.load_snmp_config()

        # One poller for the whole run: it remembers the previous counters,
        # which it needs to work out bits per second.
        self.poller = snmp.SNMPPoller(target=target, if_name=if_name, community=community)

        # How often scheduler.py should call run().
        self.interval_s = load_interval_s("snmp", DEFAULT_INTERVAL_S)

        # The latest poll result and push error (None = no error), for state.py.
        self.latest: Optional[snmp.SNMPPollResult] = None
        self.latest_push_error: Optional[str] = None

        # The last HISTORY_READINGS results. A deque with maxlen drops the
        # oldest result by itself when a new one is added.
        self.history = deque(maxlen=HISTORY_READINGS)

    def recent(self, n: int) -> List[snmp.SNMPPollResult]:
        """The last n results, oldest first (fewer if we do not have n yet).
        Returns a COPY, so the other lane can read it safely."""
        items = list(self.history)
        return items[-n:] if n > 0 else []

    def run(self) -> snmp.SNMPPollResult:
        """Do one poll: measure -> translate -> send. Never raises."""

        result = self.poller.poll_once()             # 1. measure
        lines = snmp_lines.to_lines(result)          # 2. translate
        push_error = victoria.push(lines)            # 3. send

        self.history.append(result)                  # 4. remember
        self.latest = result
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: 3 polls, one interval apart, printing what was sent,
    # then what the history holds.
    import time

    job = SnmpJob()
    print(f"SNMP job: target={job.poller.target}, interface={job.poller.if_name}, "
          f"every {job.interval_s:g} s, keeps last {HISTORY_READINGS} readings\n")

    for i in range(3):
        r = job.run()
        print(r)
        for line in snmp_lines.to_lines(r):
            print("   ", line)
        print("    -> pushed" if job.latest_push_error is None
              else f"    -> push FAILED: {job.latest_push_error}")
        print()
        if i < 2:
            time.sleep(job.interval_s)

    print(f"history holds {len(job.history)} reading(s)")
    print("recent(2) in_bps / out_bps:",
          [(x.in_bps, x.out_bps) for x in job.recent(2)])