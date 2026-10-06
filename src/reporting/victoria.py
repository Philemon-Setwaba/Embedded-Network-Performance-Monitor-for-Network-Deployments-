"""
src/reporting/victoria.py

The "translator and sender" of the monitor.

It does two jobs:
  1. TRANSLATE: turn a measurement result (e.g. a PingResult) into
     lines of text that VictoriaMetrics understands.
  2. SEND: post those lines to VictoriaMetrics running on the Pi itself
     (http://localhost:8428).

Every line has 4 parts:

    ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
    └── name ─┘└────────────── labels ─────────────────┘ └val┘└─ time ────┘

  - name   : what was measured
  - labels : TEXT that says which device / target it belongs to
  - value  : must be a NUMBER (True/False -> 1/0, statuses -> code numbers)
  - time   : milliseconds since 1 January 1970 (UTC)

This format was tested by hand on the Pi with curl and it worked.

Only ping is handled for now. The other measurements will be added
one at a time, each with its own *_to_lines() function.
"""

# urllib is built into Python - it can send HTTP requests,
# so no extra package has to be installed.
import urllib.error
import urllib.request

# datetime is used to turn the timestamp text into milliseconds.
from datetime import datetime

# These are only "type hints" - they describe what goes in and out of
# each function. They do not change how the code runs.
from typing import Dict, List, Optional

# ping.py - we need its ERROR_* names for the status table below.
from src.measurement import ping as ping_module


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Where to send the lines.
#   localhost  = this same machine (the Pi)
#   8428       = the port VictoriaMetrics listens on (see victoria.service)
#   /api/v1/import/prometheus = VictoriaMetrics' address for incoming data
VM_IMPORT_URL = "http://localhost:8428/api/v1/import/prometheus"

# Put on every line as  device="pi5"  so that, if more Pis are added
# later, their data can be told apart.
DEVICE = "pi5"

# Error messages are stored in a label. Keep them at most 200 characters,
# so one long, strange message cannot create a huge label.
MAX_LABEL_LEN = 200

# If VictoriaMetrics does not answer within 5 seconds, give up on this push.
PUSH_TIMEOUT_S = 5.0

# Table that turns ping's error TEXT into a NUMBER for ping_status.
# (VictoriaMetrics can only store numbers as values.)
# In Grafana, "value mappings" turn the numbers back into these words.
PING_STATUS_CODES = {
    None: 0,                              # ok - no error
    ping_module.ERROR_NO_REPLY: 1,        # "no_reply"    - sent, but no reply
    ping_module.ERROR_PING_ERROR: 2,      # "ping_error"  - e.g. unknown name
    ping_module.ERROR_TIMEOUT: 3,         # "timeout"     - ping got stuck, Python stopped it
    ping_module.ERROR_NO_RTT: 4,          # "no_rtt"      - no time found in ping's output
    ping_module.ERROR_EXEC_FAILED: 5,     # "exec_failed" - ping could not start
}

# Safety net: if ping.py ever gives an error that is not in the table,
# store -1 instead of crashing. -1 on the dashboard means "unknown".
UNKNOWN_STATUS_CODE = -1


# ---------------------------------------------------------------------------
# SMALL HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def to_ms(iso_timestamp: str) -> int:
    """
    Turn the timestamp text from a result into milliseconds.

    Example:
        '2026-10-05T16:15:53.458147+00:00'  ->  1791216953458
    """
    # fromisoformat() reads the text as a date and time.
    # .timestamp() gives seconds since 1970, e.g. 1791216953.458147
    # * 1000 gives milliseconds, and int() drops the decimals.
    return int(datetime.fromisoformat(iso_timestamp).timestamp() * 1000)


def _escape(text: str) -> str:
    """
    Make text safe to put inside a label's quotes.

    Inside "...", three characters have special meaning, so they must be
    written differently, or VictoriaMetrics would misread the line:
        \\       (backslash)    ->  \\\\
        "        (double quote) ->  \\"
        newline                 ->  \\n
    """
    return (
        text.replace("\\", "\\\\")     # do backslash first, or we'd double the others
            .replace('"', '\\"')
            .replace("\n", "\\n")
    )


def _line(name: str, labels: Dict[str, str], value: float, ts_ms: int) -> str:
    """
    Build ONE line.

    Example:
        _line("ping_rtt_ms",
              {"device": "pi5", "target": "8.8.8.8", "count": "5"},
              19.2,
              1791216953458)
    gives:
        ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
    """
    # Turn the labels dictionary into:  device="pi5",target="8.8.8.8",count="5"
    label_text = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())

    # Put the 4 parts together. ({{ and }} print a single { and }.)
    return f"{name}{{{label_text}}} {value} {ts_ms}"


def _short_text(text: str) -> str:
    """
    Keep only the first line of an error message, at most 200 characters.

    Example:
        'ping: doesnotexist.invalid: Name or service not known'
    stays the same (it is one short line).
    """
    if not text.strip():
        return ""
    first_line = text.strip().splitlines()[0]
    return first_line[:MAX_LABEL_LEN]


# ---------------------------------------------------------------------------
# PING: translate one PingResult into lines
# ---------------------------------------------------------------------------

def ping_to_lines(result: "ping_module.PingResult", count: int) -> List[str]:
    """
    Turn one PingResult into VictoriaMetrics lines.

    Example input (your real 'unknown name' test):
        PingResult(timestamp='2026-10-05T16:16:40.946004+00:00',
                   target='doesnotexist.invalid', reachable=False,
                   rtt_ms=None, loss_pct=100.0, error='ping_error',
                   error_detail='ping: doesnotexist.invalid: Name or service not known')

    Output:
        ping_status{...} 2 1791217000946
        ping_reachable{...} 0 1791217000946
        ping_loss_pct{...} 100.0 1791217000946
        ping_error_info{...,error_detail="ping: ... not known"} 1 1791217000946
    (no ping_rtt_ms line, because rtt_ms is None)
    """

    # The time, as milliseconds. Every line from this result gets the same time.
    ts = to_ms(result.timestamp)

    # The labels that go on every line from this result.
    # count is turned into text ("5") because labels are always text.
    labels = {"device": DEVICE, "target": result.target, "count": str(count)}

    # Look up the error in the table above to get its number.
    # e.g. None -> 0, "no_reply" -> 1, "ping_error" -> 2
    status = PING_STATUS_CODES.get(result.error, UNKNOWN_STATUS_CODE)

    # These three lines are ALWAYS sent.
    lines = [
        _line("ping_status", labels, status, ts),                    # 0..5
        _line("ping_reachable", labels, int(result.reachable), ts),  # True -> 1, False -> 0
        _line("ping_loss_pct", labels, result.loss_pct, ts),         # 0..100
    ]

    # Only send ping_rtt_ms when there was a reply.
    # No reply means rtt_ms is None, and None is not a number.
    if result.rtt_ms is not None:
        lines.append(_line("ping_rtt_ms", labels, result.rtt_ms, ts))

    # Only send ping_error_info when there is an error message.
    # The message goes into an extra label "error_detail"; the value is just 1.
    # dict(labels, error_detail=...) = a copy of labels with one extra label.
    if result.error_detail:
        info_labels = dict(labels, error_detail=_short_text(result.error_detail))
        lines.append(_line("ping_error_info", info_labels, 1, ts))

    return lines


# ---------------------------------------------------------------------------
# SEND: post lines to VictoriaMetrics
# ---------------------------------------------------------------------------

def push(lines: List[str]) -> Optional[str]:
    """
    Send lines to VictoriaMetrics.

    Returns:
        None            -> it worked
        a text message  -> it failed, and why (e.g. VictoriaMetrics is off)

    It never crashes. If sending fails, the measurements must keep running.
    """

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