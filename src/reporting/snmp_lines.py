"""
src/reporting/snmp_lines.py

TRANSLATE one SNMPPollResult (from src/measurement/snmp.py) into lines of
text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Lines produced (labels on every line: device, target, if_index):
    snmp_status             0 / 1 / 2  always - see STATUS CODES below
    snmp_reachable          1 / 0      always (1 = the agent sent a reply)
    snmp_in_octets_total    bytes      if counters were read (raw counter)
    snmp_out_octets_total   bytes      if counters were read (raw counter)
    snmp_in_bps             bits/s     from the 2nd good poll on (needs a previous poll)
    snmp_out_bps            bits/s     from the 2nd good poll on
    snmp_counter_bits       64 / 32    if counters were read
    snmp_error_info         1          only if there is an error message
                                       (the message is in the label error_detail)

Why store the raw counters as well as bps:
  the counters are what was actually measured; bps is calculated from
  them. Keeping both means the bps can be checked later.
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# snmp.py - only needed for the type hint below.
from src.measurement import snmp as snmp_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


# ---------------------------------------------------------------------------
# STATUS CODES
# ---------------------------------------------------------------------------

# SNMPPollResult has no status field, so it is worked out here:
STATUS_OK = 0           # counters were read
STATUS_BAD_REPLY = 1    # agent replied, but no usable counters (e.g. wrong if_index)
STATUS_NO_REPLY = 2     # no reply at all (agent down, wrong community, snmpget missing)
# In Grafana, "value mappings" turn the numbers back into words.


# ---------------------------------------------------------------------------
# TRANSLATE
# ---------------------------------------------------------------------------

def to_lines(result: "snmp_module.SNMPPollResult") -> List[str]:
    """
    Turn one SNMPPollResult into VictoriaMetrics lines.

    Example input (a 2nd good poll):
        SNMPPollResult(target='127.0.0.1', if_index=3, reachable=True,
                       in_octets=529289, out_octets=319157,
                       in_bps=1234.56, out_bps=789.0,
                       counter_bits=64, error_detail=None)
    Output:
        snmp_status{device="pi5",target="127.0.0.1",if_index="3"} 0 ...
        snmp_reachable{...} 1 ...
        snmp_in_octets_total{...} 529289 ...
        snmp_out_octets_total{...} 319157 ...
        snmp_in_bps{...} 1234.56 ...
        snmp_out_bps{...} 789.0 ...
        snmp_counter_bits{...} 64 ...
    """

    # The time, as milliseconds. Every line from this poll gets the same time.
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line.
    # if_index is turned into text ("3") because labels are always text.
    labels = {
        "device": line_format.DEVICE,
        "target": result.target,
        "if_index": str(result.if_index),
    }

    # Work out the status number (see STATUS CODES above).
    if result.in_octets is not None:
        status = STATUS_OK
    elif result.reachable:
        status = STATUS_BAD_REPLY
    else:
        status = STATUS_NO_REPLY

    # These two lines are ALWAYS sent.
    lines = [
        line_format.line("snmp_status", labels, status, ts),
        line_format.line("snmp_reachable", labels, int(result.reachable), ts),  # True -> 1
    ]

    # The numbers - only sent if they exist (None is not a number).
    # bps is None on the first good poll, because it needs a previous poll.
    numbers = [
        ("snmp_in_octets_total", result.in_octets),
        ("snmp_out_octets_total", result.out_octets),
        ("snmp_in_bps", result.in_bps),
        ("snmp_out_bps", result.out_bps),
        ("snmp_counter_bits", result.counter_bits),
    ]
    for name, value in numbers:
        if value is not None:
            lines.append(line_format.line(name, labels, value, ts))

    # Only send snmp_error_info when there is an error message.
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("snmp_error_info", info_labels, 1, ts))

    return lines