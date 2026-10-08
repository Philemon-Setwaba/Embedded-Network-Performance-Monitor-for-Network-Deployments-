"""
src/scheduler.py

The TIMER of the monitor. It only decides WHEN each job runs.

Each job (in src/jobs/) does the real work:
    measure -> translate (*_lines.py) -> send (victoria.py) -> keep latest

How it works:
  - every job has an interval_s (e.g. SNMP every 10 s)
  - the scheduler runs each job when it is due, ONE AT A TIME
    (never two at once - active tests must not overlap)
  - then it sleeps until the next job is due

For now only the SNMP job is added. The other jobs (health, Wi-Fi, ping,
TCP, traceroute, iperf3) and state.py will be added one at a time.

How to run (from the project folder ~/embedded-network-monitor):
    python3 -m src.scheduler
Stop it with Ctrl+C.
"""

import time
from datetime import datetime, timezone

from src.jobs.snmp_job import SnmpJob


def build_jobs():
    """The list of jobs the scheduler runs. Add new jobs here."""
    return [
        SnmpJob(),      # every 10 s (config.json "snmp.interval_s"), in every state
    ]


def run_job(job) -> None:
    """Run one job and print one short line. A crash in a job never stops the scheduler."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    try:
        job.run()
    except Exception as exc:  # the jobs should never raise, but just in case
        print(f"{now} UTC  {job.name:<10} CRASHED: {exc}")
        return

    if job.latest_push_error is None:
        print(f"{now} UTC  {job.name:<10} pushed")
    else:
        print(f"{now} UTC  {job.name:<10} push FAILED: {job.latest_push_error}")


def main() -> None:
    jobs = build_jobs()

    print("Scheduler started. Jobs:")
    for job in jobs:
        print(f"  {job.name:<10} every {job.interval_s:g} s")
    print("Press Ctrl+C to stop.\n")

    # When each job is next due. time.monotonic() is a clock that never jumps
    # (unlike the wall clock, which can change when NTP corrects it).
    # All jobs are due straight away at the start.
    start = time.monotonic()
    next_due = {job.name: start for job in jobs}

    try:
        while True:
            # Run every job that is due now, one after another.
            for job in jobs:
                if time.monotonic() >= next_due[job.name]:
                    run_job(job)
                    # Next run = one interval after the planned time, so the
                    # timing does not drift. If the job was late (e.g. a long
                    # job before it), do not try to "catch up" with a burst.
                    next_due[job.name] = max(next_due[job.name] + job.interval_s,
                                             time.monotonic())

            # Sleep until the next job is due.
            wait = min(next_due.values()) - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    except KeyboardInterrupt:
        # Ctrl+C: stop with a short message instead of a long Python error.
        print("\nStopped.")


# Only run main() when this file is started directly (python3 -m src.scheduler).
if __name__ == "__main__":
    main()