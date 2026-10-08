"""
src/reporting/tcp_lines.py

TRANSLATE one TCPReachabilityResult (from src/measurement/tcp_reachability.py)
into lines of text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Lines produced (labels on every line: device, target, port):
    tcp_status            0..4    always - status as a number (table below)
    tcp_reachable         1 / 0   always (1 = target replied: connected OR refused)
    tcp_connect_time_ms   ms      only if the target replied
    tcp_error_info        1       only if there is an error message
                                  (the message is in the label error_detail)

Note: "refused" has an error message but reachable=1 - the target itself
replied "port closed", so the path through the backhaul is working.
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# tcp_reachability.py - we need its STATUS_* names for the table below.
from src.measurement import tcp_reachability as tcp_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


# ---------------------------------------------------------------------------
# STATUS TABLE
# ---------------------------------------------------------------------------

# Table that turns the status TEXT into a NUMBER for tcp_status.
# (VictoriaMetrics can only store numbers as values.)
# In Grafana, "value mappings" turn the numbers back into these words.
STATUS_CODES = {
    tcp_module.STATUS_CONNECTED: 0,             # target accepted the connection
    tcp_module.STATUS_REFUSED: 1,               # target replied "port closed": path is up
    tcp_module.STATUS_TIMEOUT: 2,               # no reply: path down or filtered
    tcp_module.STATUS_NETWORK_UNREACHABLE: 3,   # Pi's own network is down
    tcp_module.STATUS_OTHER_ERROR: 4,           # any other failure (see error_detail)
}

# Safety net: a status not in the table is stored as -1 ("unknown").
UNKNOWN_STATUS_CODE = -1


# ---------------------------------------------------------------------------
# TRANSLATE
# ---------------------------------------------------------------------------

def to_lines(result: "tcp_module.TCPReachabilityResult") -> List[str]:
    """
    Turn one TCPReachabilityResult into VictoriaMetrics lines.

    Example input (Wi-Fi off):
        TCPReachabilityResult(target='1.1.1.1', port=443, reachable=False,
                              connect_time_ms=None, status='network_unreachable',
                              error_detail='[Errno 101] Network is unreachable')
    Output:
        tcp_status{device="pi5",target="1.1.1.1",port="443"} 3 ...
        tcp_reachable{...} 0 ...
        tcp_error_info{...,error_detail="[Errno 101] Network is unreachable"} 1 ...
    (no tcp_connect_time_ms line, because connect_time_ms is None)
    """

    # The time, as milliseconds. Every line from this result gets the same time.
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line.
    # port is turned into text ("443") because labels are always text.
    labels = {"device": line_format.DEVICE, "target": result.target, "port": str(result.port)}

    # Look up the status in the table above to get its number.
    status = STATUS_CODES.get(result.status, UNKNOWN_STATUS_CODE)

    # These two lines are ALWAYS sent.
    lines = [
        line_format.line("tcp_status", labels, status, ts),
        line_format.line("tcp_reachable", labels, int(result.reachable), ts),  # True -> 1
    ]

    # Only send the time when the target replied (None is not a number).
    if result.connect_time_ms is not None:
        lines.append(line_format.line("tcp_connect_time_ms", labels, result.connect_time_ms, ts))

    # Only send tcp_error_info when there is an error message.
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("tcp_error_info", info_labels, 1, ts))

    return lines