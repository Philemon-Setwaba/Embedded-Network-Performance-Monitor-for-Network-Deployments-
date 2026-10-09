"""
src/jobs/wifi_job.py

The Wi-Fi job. Each time it runs it takes ONE Wi-Fi reading:

    measure (wifi_signal.py) -> translate (wifi_lines.py) -> send (victoria.py)

and keeps the latest result, so state.py can read it later
(events C1-C3: Wi-Fi down, weak signal, switched access point).

It does NOT decide WHEN to run - scheduler.py does that, using interval_s.

Why every 2 s, in every state (tests on the Pi 5, eduroam, 2026-10-09):
  - C1 (Wi-Fi drop) needs a short interval: a reading every T seconds only
    surely catches a drop that lasts LONGER than T. In a test with 3 Wi-Fi
    interruptions the outages lasted 5.6, 6.7 and 9.1 s - a 10 s interval
    could miss them all; at 2 s each is seen by at least two readings.
  - the driver updates the values fast (signal changed every ~0.8 s when
    quiet, ~0.6 s under iperf3 traffic), so 2 s readings are real changes,
    not repeats.
  - C2 (weak signal) and C3 (AP switch) change slowly (quiet test: signal
    range only 4 dB over 5 min), so 2 s is more than enough for them.
  - measured cost: 0.002 s per reading (mean of 10), about 0.1 % of the time.
  - 2 goes into 10, so every 5th Wi-Fi reading is in the same round as
    SNMP and health.
  - it only asks the Pi's own Wi-Fi driver (iw), so it does not need the backhaul.
Limits: the drops were made by hand (Wi-Fi off/on); natural drops may be
shorter. A reading is a SAMPLE: the signal can change between readings.

Settings: the "wifi" section of config.json, e.g.
    "wifi": {"interface": "wlan0", "interval_s": 2}
"""

import json
from typing import Optional

# measure
from src.measurement import wifi_signal
# translate
from src.reporting import wifi_lines
# send
from src.reporting import victoria
# config.json location and interval reader
from src.jobs.job_config import CONFIG_PATH, load_interval_s


# Used when config.json has no "wifi" section (or no setting in it).
DEFAULT_INTERVAL_S = 2.0
DEFAULT_INTERFACE = wifi_signal.DEFAULT_INTERFACE   # "wlan0"


def _load_interface() -> str:
    """Read "interface" from the "wifi" section of config.json (or use wlan0)."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("wifi", {}).get("interface", DEFAULT_INTERFACE)
    except FileNotFoundError:
        return DEFAULT_INTERFACE


class WifiJob:
    """Takes one Wi-Fi reading each time run() is called."""

    name = "wifi"

    def __init__(self):
        # Which wireless interface to read, e.g. "wlan0".
        self.interface = _load_interface()

        # How often scheduler.py should call run().
        self.interval_s = load_interval_s("wifi", DEFAULT_INTERVAL_S)

        # The latest reading and push error (None = no error), for state.py.
        self.latest: Optional[wifi_signal.WifiResult] = None
        self.latest_push_error: Optional[str] = None

    def run(self) -> wifi_signal.WifiResult:
        """Take one reading: measure -> translate -> send. Never raises."""

        result = wifi_signal.read_wifi(self.interface)   # 1. measure
        lines = wifi_lines.to_lines(result)              # 2. translate
        push_error = victoria.push(lines)                # 3. send

        self.latest = result                             # 4. remember
        self.latest_push_error = push_error
        return result


if __name__ == "__main__":
    # Manual test: 3 readings, one interval apart, printing what was sent.
    import time

    job = WifiJob()
    print(f"Wi-Fi job: interface={job.interface}, every {job.interval_s:g} s\n")

    for _ in range(3):
        r = job.run()
        print(r)
        for line in wifi_lines.to_lines(r):
            print("   ", line)
        print("    -> pushed" if job.latest_push_error is None
              else f"    -> push FAILED: {job.latest_push_error}")
        print()
        time.sleep(job.interval_s)