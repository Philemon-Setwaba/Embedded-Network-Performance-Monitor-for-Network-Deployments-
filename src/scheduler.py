"""
src/scheduler.py

The TIMER of the monitor. It only decides WHEN each job runs.

Each job (in src/jobs/) does the real work:
    measure -> translate (*_lines.py) -> send (victoria.py) -> keep latest

TWO LANES, each in its own thread, running side by side:

    passive lane:  snmp (10 s), health (10 s), wifi (2 s)
                   only read local values - never wait for an active test
    active lane:   ping (10 s), tcp (only after a ping with no reply),
                   state (after each new ping),
                   traceroute (only when the backhaul state calls for it:
                   on down, every 30 min while down, on back up,
                   hourly baseline while up)
                   (later: iperf3)
                   send traffic over the link - ONE AT A TIME, never overlapping

The state job (src/jobs/state.py) does not measure anything: it reads the
latest ping, tcp and wifi results and decides backhaul up / down / local
link down. It prints a "STATE old -> new (why)" line only when the state
CHANGES. (How full the link is, is shown in Grafana from the raw SNMP bps.)

Why two lanes: an active test can take a long time (ping ~4-6 s, iperf3
10-20 s, traceroute up to ~12 s measured). With one lane, SNMP / health / Wi-Fi would
pause for that long - exactly when you most want to see traffic and CPU.

Inside each lane the rule is the same as before:
  - every job has an interval_s
  - the lane runs each job when it is due, one after another
  - then it waits until its next job is due
  - timing does not drift; a late run does not cause a burst of catch-up runs

Why it is safe: each job belongs to ONE lane only, so no two threads ever
run the same job. The state job reads wifi's latest result from the other
lane, but only READS it. Printing uses a lock so lines from the two lanes never mix.

How to run (from the project folder ~/embedded-network-monitor):
    python3 -m src.scheduler
Stop it with Ctrl+C (it may take a few seconds: a running ping is allowed
to finish first).
"""

import threading
import time
from datetime import datetime, timezone

from src.jobs.snmp_job import SnmpJob
from src.jobs.health_job import HealthJob
from src.jobs.wifi_job import WifiJob
from src.jobs.ping_job import PingJob
from src.jobs.tcp_job import TcpJob
from src.jobs.state import StateJob
from src.jobs.traceroute_job import TracerouteJob


# Only one thread may print at a time, so lines never get mixed.
PRINT_LOCK = threading.Lock()


def log(lane_name: str, message: str) -> None:
    """Print one line: time (UTC, with milliseconds), lane, message."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    with PRINT_LOCK:
        print(f"{now} UTC  [{lane_name:<7}] {message}")


class Lane:
    """
    One lane = a list of jobs, run one at a time, each when it is due.
    (This is the same loop the scheduler had before, inside a class,
    so we can make two of them.)
    """

    def __init__(self, name: str, jobs, stop_event: threading.Event):
        self.name = name
        self.jobs = jobs
        # Shared "stop" signal: when it is set, the lane stops.
        self.stop_event = stop_event

    def run_job(self, job) -> None:
        """Run one job and print one short line. A crash never stops the lane."""
        try:
            job.run()
        except Exception as exc:  # the jobs should never raise, but just in case
            log(self.name, f"{job.name:<10} CRASHED: {exc}")
            return

        if job.latest_push_error is None:
            log(self.name, f"{job.name:<10} pushed")
        else:
            log(self.name, f"{job.name:<10} push FAILED: {job.latest_push_error}")

    def run(self) -> None:
        """The lane's loop. Runs until the stop signal is set."""

        # When each job is next due. time.monotonic() is a clock that never
        # jumps (unlike the wall clock, which can change when NTP corrects it).
        # All jobs are due straight away at the start.
        start = time.monotonic()
        next_due = {job.name: start for job in self.jobs}

        while not self.stop_event.is_set():
            # Run every job that is due now, one after another.
            for job in self.jobs:
                if self.stop_event.is_set():
                    break
                if time.monotonic() >= next_due[job.name]:
                    # Some jobs only run when needed (e.g. TCP only after a
                    # failed ping). They have should_run(); others always run.
                    if getattr(job, "should_run", lambda: True)():
                        self.run_job(job)
                    # Next run = one interval after the planned time, so the
                    # timing does not drift. If the job was late, do not try
                    # to "catch up" with a burst.
                    next_due[job.name] = max(next_due[job.name] + job.interval_s,
                                             time.monotonic())

            # Wait until the next job is due - or until "stop" is set.
            # (stop_event.wait returns early on stop, unlike time.sleep.)
            wait = min(next_due.values()) - time.monotonic()
            if wait > 0:
                self.stop_event.wait(wait)


def build_lanes(stop_event: threading.Event):
    """The two lanes and their jobs. Add new jobs here."""

    # Jobs that other jobs read from are made first.
    wifi_job = WifiJob()
    ping_job = PingJob()
    tcp_job = TcpJob(ping_job)
    state_job = StateJob(ping_job, tcp_job, wifi_job,
                         log=lambda m: log("active", m))

    passive = Lane("passive", [
        SnmpJob(),        # every 10 s (config.json "snmp.interval_s")
        HealthJob(),      # every 10 s (config.json "health.interval_s"), same round as SNMP
        wifi_job,         # every 2 s  (config.json "wifi.interval_s")
    ], stop_event)

    active = Lane("active", [
        ping_job,         # every 10 s (config.json "ping.interval_s"), 5 packets
        tcp_job,          # right after ping, ONLY when that ping got no reply
        state_job,        # right after ping and tcp, once per new ping
        TracerouteJob(state_job),  # right after state, ONLY when the state calls for it
    ], stop_event)

    return [passive, active]


def main() -> None:
    stop_event = threading.Event()
    lanes = build_lanes(stop_event)

    print("Scheduler started. Lanes and jobs:")
    for lane in lanes:
        for job in lane.jobs:
            when = " (only when needed)" if hasattr(job, "should_run") else ""
            print(f"  [{lane.name:<7}] {job.name:<10} every {job.interval_s:g} s{when}")
    print("Press Ctrl+C to stop.\n")

    # One thread per lane. daemon=True: if the main program ends, they end too.
    threads = [threading.Thread(target=lane.run, name=lane.name, daemon=True)
               for lane in lanes]
    for thread in threads:
        thread.start()

    try:
        # The main thread just waits. (Ctrl+C always arrives in the main thread.)
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping... (letting any running job finish)")
        stop_event.set()
        for thread in threads:
            thread.join(timeout=15)
        print("Stopped.")


# Only run main() when this file is started directly (python3 -m src.scheduler).
if __name__ == "__main__":
    main()