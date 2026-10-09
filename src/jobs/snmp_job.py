"""
src/jobs/snmp_job.py

The SNMP job. Each time it runs it does ONE poll:

    measure  (snmp.py)  ->  translate (snmp_lines.py)  ->  send (victoria.py)

and keeps the latest result, so state.py can read it later
(e.g. for the "link overwhelmed" check).

It does NOT decide WHEN to run - scheduler.py does that, using interval_s.

Why SNMP runs every 10 s, in every state (also when the backhaul is down):
  - it only reads counters on the Pi (127.0.0.1) or a local router, so it
    does not need the backhaul;
  - it shows what happens to traffic during an outage, and when it starts/ends;
  - the readings are stored on the Pi, so there is no gap in the record;
  - the "overwhelmed" and "quiet before iperf3" checks need recent readings.
The interval must be at least the target's counter update time
(measured about 3.07 s on the Pi).

Settings: the "snmp" section of config.json, e.g.
    "snmp": {"target": "127.0.0.1", "if_index": 3, "community": "public",
             "interval_s": 10}
"""

from typing import Optional

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


class SnmpJob:
    """Polls SNMP once each time run() is called."""

    name = "snmp"

    def __init__(self):
        # Target, interface number and community from config.json (snmp.py reads them).
        target, if_index, community = snmp.load_snmp_config()

        # One poller for the whole run: it remembers the previous counters,
        # which it needs to work out bits per second.
        self.poller = snmp.SNMPPoller(target=target, if_index=if_index, community=community)

        # How often scheduler.py should call run().
        self.interval_s = load_interval_s("snmp", DEFAULT_INTERVAL_S)

        # The latest poll result and push error (None = no error), for state.py.
        self.latest: Optional[snmp.SNMPPollResult] = None
        self.latest_push_error: Optional[str] = None

    def run(self) -> snmp.SNMPPollResult:
        """Do one poll: measure -> translate -> send. Never raises."""

        result = self.poller.poll_once()             # 1. measure
        lines = snmp_lines.to_lines(result)          # 2. translate
        push_error = victoria.push(lines)            # 3. send

        self.latest = result                         # 4. remember
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: 3 polls, one interval apart, printing what was sent.
    import time

    job = SnmpJob()
    print(f"SNMP job: target={job.poller.target}, if_index={job.poller.if_index}, "
          f"every {job.interval_s:g} s\n")

    for _ in range(3):
        r = job.run()
        print(r)
        for line in snmp_lines.to_lines(r):
            print("   ", line)
        print("    -> pushed" if job.latest_push_error is None
              else f"    -> push FAILED: {job.latest_push_error}")
        print()
        time.sleep(job.interval_s)