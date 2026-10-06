"""
src/scheduler.py

The "timer" of the monitor. It runs ping every 30 seconds and, after
each ping, hands the result to victoria.py, which sends it to
VictoriaMetrics on the Pi.

The whole path:
    ping.py  ->  scheduler.py  ->  victoria.py  ->  VictoriaMetrics  ->  Grafana
    (measure)    (when to run)     (translate + send)  (store)            (graph)

For now only ping is scheduled. The other measurements will be added
here one by one, once ping works end to end.

How to run (from the project folder ~/embedded-network-monitor):
    python3 -m src.scheduler
Stop it with Ctrl+C.
"""

# PingMeasurer is the class in src/measurement/ping.py that runs `ping`.
from src.measurement.ping import PingMeasurer

# victoria is our own file src/reporting/victoria.py.
# We use two functions from it:
#   victoria.ping_to_lines()  -> turns a PingResult into text lines
#   victoria.push()           -> sends those lines to VictoriaMetrics
from src.reporting import victoria


# ---------------------------------------------------------------------------
# SETTINGS - change these to change what is measured and how often
# ---------------------------------------------------------------------------

# Who we ping. 8.8.8.8 is Google's public DNS server: always on,
# and outside the campus network, so it tests the path to the internet.
PING_TARGET = "8.8.8.8"

# How many packets per measurement.
# With 5 packets, loss can be 0, 20, 40, 60, 80 or 100 %.
# (ping.py's own default is 1; we pass 5 here.)
PING_COUNT = 5

# Seconds from the START of one measurement to the START of the next.
# 30 s is the default interval from the spec (SP-1).
PING_INTERVAL_S = 30.0


# ---------------------------------------------------------------------------
# WHAT TO DO WITH EACH RESULT
# ---------------------------------------------------------------------------

def handle_ping_result(result) -> None:
    """
    ping.py calls this function automatically after EVERY measurement,
    and gives it the PingResult, for example:

        PingResult(timestamp='2026-10-05T16:15:53.458147+00:00',
                   target='8.8.8.8', reachable=True, rtt_ms=19.2,
                   loss_pct=0.0, error=None, error_detail=None)
    """

    # Step 1: translate the PingResult into VictoriaMetrics lines, e.g.
    #   ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
    lines = victoria.ping_to_lines(result, PING_COUNT)

    # Step 2: send the lines to VictoriaMetrics.
    # push() returns None if it worked, or an error message (text) if not.
    error = victoria.push(lines)

    # Step 3: print everything, so we can watch it work in the terminal.
    print(result)                    # the raw PingResult
    for line in lines:               # each line that was sent
        print("   ", line)
    if error is None:
        print("    -> pushed")       # VictoriaMetrics accepted it
    else:
        print(f"    -> push FAILED: {error}")   # e.g. VictoriaMetrics not running
    print()                          # empty line between measurements


# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------

def main() -> None:
    # Create the ping measurer with our settings.
    measurer = PingMeasurer(target=PING_TARGET, count=PING_COUNT)

    print(
        f"Pinging {PING_TARGET} with {PING_COUNT} packets every "
        f"{PING_INTERVAL_S:g} s. Press Ctrl+C to stop.\n"
    )

    try:
        # run_continuous() is the loop that lives in ping.py:
        #   1. ping
        #   2. call on_result (our handle_ping_result above)
        #   3. sleep for whatever is left of the 30 s
        #   4. repeat forever
        # We just tell it HOW OFTEN and WHAT TO DO with each result.
        measurer.run_continuous(
            interval_s=PING_INTERVAL_S,
            on_result=handle_ping_result,
        )
    except KeyboardInterrupt:
        # Pressing Ctrl+C raises KeyboardInterrupt.
        # Catch it so the program ends with a short message
        # instead of a long Python error.
        print("\nStopped.")


# This line means: only run main() when this file is started directly
# (python3 -m src.scheduler), not when another file imports it.
if __name__ == "__main__":
    main()