"""
src/reporting/victoria.py

The SENDER. Every push goes to TWO places:

    1. LOCAL VictoriaMetrics on the Pi (http://localhost:8428)
       - stores everything, feeds the local Grafana dashboard
       - works even when the backhaul is down
    2. vmagent on the Pi (http://127.0.0.1:8429) = STORE-AND-FORWARD
       - sends a copy to the REMOTE VictoriaMetrics (laptop / central server)
       - if the remote cannot be reached, vmagent keeps the data and
         sends it later (see infrastructure/vmagent/vmagent.service)

Why two separate pushes (vmagent BESIDE local storage, not in front of it):
    if vmagent is stopped or broken, the LOCAL push still works, so no
    measurement is lost on the Pi. The two pushes do not depend on each other.

The lines are made by the *_lines.py files (ping_lines.py, health_lines.py,
wifi_lines.py, tcp_lines.py, snmp_lines.py, iperf_lines.py,
traceroute_lines.py), using the helpers in line_format.py.
This file only SENDS them.
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

# 1. Local VictoriaMetrics.
#   localhost  = this same machine (the Pi)
#   8428       = the port VictoriaMetrics listens on (see victoria.service)
#   /api/v1/import/prometheus = VictoriaMetrics' address for incoming data
VM_IMPORT_URL = "http://localhost:8428/api/v1/import/prometheus"

# 2. vmagent (store-and-forward). Same line format, same path.
#   127.0.0.1:8429 = where vmagent listens (-httpListenAddr in vmagent.service)
# Set to None to switch forwarding off (local storage keeps working).
VMAGENT_IMPORT_URL: Optional[str] = "http://127.0.0.1:8429/api/v1/import/prometheus"

# If a receiver does not answer within 5 seconds, give up on this push.
# (Both are on the Pi itself, so a normal answer takes milliseconds.)
PUSH_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# SEND ONE: post lines to one address
# ---------------------------------------------------------------------------

def _post(url: str, body: bytes, name: str) -> Optional[str]:
    """
    POST the body to one address.

    Returns None if it worked, otherwise a short message that starts with
    `name` ("local" or "forward"), so the log shows WHICH push failed.
    Never raises.
    """
    # The same as the curl command we tested by hand:
    #   curl -X POST <url> --data-binary '...'
    request = urllib.request.Request(url, data=body, method="POST")

    try:
        # Send it, and wait up to PUSH_TIMEOUT_S seconds for an answer.
        with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_S) as response:
            # HTTP codes 200-299 mean "OK".
            if 200 <= response.status < 300:
                return None
            return f"{name}: answered HTTP {response.status}"

    except urllib.error.HTTPError as exc:
        # It answered, but with an error code (e.g. 400 = bad line).
        # exc.read() is the receiver's own explanation.
        reason = exc.read().decode(errors="replace").strip()
        return f"{name}: answered HTTP {exc.code}: {reason}"

    except (urllib.error.URLError, OSError) as exc:
        # Could not reach it at all,
        # e.g. "Connection refused" when it is not running.
        return f"{name}: could not reach it: {exc}"


# ---------------------------------------------------------------------------
# SEND: post lines to local VictoriaMetrics AND to vmagent
# ---------------------------------------------------------------------------

def push(lines: List[str]) -> Optional[str]:
    """
    Send lines to local VictoriaMetrics, then to vmagent.

    Returns:
        None            -> both worked (or there was nothing to send)
        a text message  -> at least one failed, and why, e.g.
                           "forward: could not reach it: ... refused"
                           (if both failed, both messages, joined by " | ")

    It never crashes. If sending fails, the measurements must keep running.
    The local push is always tried, whatever happens with vmagent.
    """

    # Nothing to send -> nothing to do.
    if not lines:
        return None

    # Join the lines with a newline between each one, and turn the text
    # into bytes (computers send bytes over the network, not text).
    body = ("\n".join(lines) + "\n").encode("utf-8")

    errors = []

    # 1. Local storage first: this is the copy that must never be missed.
    local_error = _post(VM_IMPORT_URL, body, "local")
    if local_error:
        errors.append(local_error)

    # 2. Forward copy (store-and-forward), if switched on.
    if VMAGENT_IMPORT_URL:
        forward_error = _post(VMAGENT_IMPORT_URL, body, "forward")
        if forward_error:
            errors.append(forward_error)

    return " | ".join(errors) if errors else None