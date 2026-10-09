"""
src/reporting/ping_lines.py

TRANSLATE one PingResult (from src/measurement/ping.py) into lines of
text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

(This is ping_to_lines() and the status table that used to be inside
victoria.py.)

Lines produced:
    ping_status      0..5    always             - error as a number (table below)
    ping_reachable   1 / 0   always
    ping_loss_pct    0..100  always
    ping_rtt_ms      ms      only if there was a reply (average)
    ping_rtt_min_ms  ms      only if there was a reply (fastest)
    ping_rtt_max_ms  ms      only if there was a reply (slowest)
    ping_rtt_mdev_ms ms      only if there was a reply (how much they vary)
    ping_error_info  1       only if there is an error message
                             (the message is in the label error_detail)

min / max / mdev are copied exactly as ping printed them. They show latency
VARIATION within one measurement (event B4) - real example from the Pi:
    avg 33.623 ms, but max 81.72 ms and mdev 24.049 ms (one slow reply)
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# ping.py - we need its ERROR_* names for the status table below.
from src.measurement import ping as ping_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


# ---------------------------------------------------------------------------
# STATUS TABLE
# ---------------------------------------------------------------------------

# Table that turns ping's error TEXT into a NUMBER for ping_status.
# (VictoriaMetrics can only store numbers as values.)
# In Grafana, "value mappings" turn the numbers back into these words.
STATUS_CODES = {
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
# TRANSLATE
# ---------------------------------------------------------------------------

def to_lines(result: "ping_module.PingResult", count: int) -> List[str]:
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
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line from this result.
    # count is turned into text ("5") because labels are always text.
    labels = {"device": line_format.DEVICE, "target": result.target, "count": str(count)}

    # Look up the error in the table above to get its number.
    # e.g. None -> 0, "no_reply" -> 1, "ping_error" -> 2
    status = STATUS_CODES.get(result.error, UNKNOWN_STATUS_CODE)

    # These three lines are ALWAYS sent.
    lines = [
        line_format.line("ping_status", labels, status, ts),                    # 0..5
        line_format.line("ping_reachable", labels, int(result.reachable), ts),  # True -> 1, False -> 0
        line_format.line("ping_loss_pct", labels, result.loss_pct, ts),         # 0..100
    ]

    # Only send ping_rtt_ms when there was a reply.
    # No reply means rtt_ms is None, and None is not a number.
    if result.rtt_ms is not None:
        lines.append(line_format.line("ping_rtt_ms", labels, result.rtt_ms, ts))

    # min / max / mdev from ping's summary line - also only when there was
    # a reply (they are None otherwise, and None is not a number).
    extra = [
        ("ping_rtt_min_ms", result.rtt_min_ms),
        ("ping_rtt_max_ms", result.rtt_max_ms),
        ("ping_rtt_mdev_ms", result.rtt_mdev_ms),
    ]
    for name, value in extra:
        if value is not None:
            lines.append(line_format.line(name, labels, value, ts))

    # Only send ping_error_info when there is an error message.
    # The message goes into an extra label "error_detail"; the value is just 1.
    # dict(labels, error_detail=...) = a copy of labels with one extra label.
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("ping_error_info", info_labels, 1, ts))

    return lines