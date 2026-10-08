"""
src/reporting/victoria.py

The SENDER. Sends lines to VictoriaMetrics running on the Pi itself
(http://localhost:8428).

The lines are made by the *_lines.py files (ping_lines.py, health_lines.py,
wifi_lines.py, tcp_lines.py, snmp_lines.py, iperf_lines.py,
traceroute_lines.py), using the helpers in line_format.py.
This file only SENDS them.

(Store-and-forward will be added here later.)
"""

# urllib is built into Python - it can send HTTP requests,
# so no extra package has to be installed.
import urllib.error
import urllib.request

# These are only "type hints" - they describe what goes in and out of
# each function. They do not change how the code runs.
from typing import List, Optional


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Where to send the lines.
#   localhost  = this same machine (the Pi)
#   8428       = the port VictoriaMetrics listens on (see victoria.service)
#   /api/v1/import/prometheus = VictoriaMetrics' address for incoming data
VM_IMPORT_URL = "http://localhost:8428/api/v1/import/prometheus"

# If VictoriaMetrics does not answer within 5 seconds, give up on this push.
PUSH_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# SEND: post lines to VictoriaMetrics
# ---------------------------------------------------------------------------

def push(lines: List[str]) -> Optional[str]:
    """
    Send lines to VictoriaMetrics.

    Returns:
        None            -> it worked (or there was nothing to send)
        a text message  -> it failed, and why (e.g. VictoriaMetrics is off)

    It never crashes. If sending fails, the measurements must keep running.
    """

    # Nothing to send -> nothing to do.
    if not lines:
        return None

    # Join the lines with a newline between each one, and turn the text
    # into bytes (computers send bytes over the network, not text).
    body = ("\n".join(lines) + "\n").encode("utf-8")

    # Prepare an HTTP POST request to VictoriaMetrics with our lines as the body.
    # This is the same as the curl command we tested by hand:
    #   curl -X POST http://localhost:8428/api/v1/import/prometheus --data-binary '...'
    request = urllib.request.Request(VM_IMPORT_URL, data=body, method="POST")

    try:
        # Send it, and wait up to PUSH_TIMEOUT_S seconds for an answer.
        with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_S) as response:
            # HTTP codes 200-299 mean "OK".
            if 200 <= response.status < 300:
                return None
            return f"VictoriaMetrics answered HTTP {response.status}"

    except urllib.error.HTTPError as exc:
        # VictoriaMetrics answered, but with an error code (e.g. 400 = bad line).
        # exc.read() is VictoriaMetrics' own explanation.
        reason = exc.read().decode(errors="replace").strip()
        return f"VictoriaMetrics answered HTTP {exc.code}: {reason}"

    except (urllib.error.URLError, OSError) as exc:
        # Could not reach VictoriaMetrics at all,
        # e.g. "Connection refused" when it is not running.
        return f"could not reach VictoriaMetrics: {exc}"