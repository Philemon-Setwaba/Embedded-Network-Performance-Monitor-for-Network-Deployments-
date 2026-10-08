"""
src/reporting/traceroute_lines.py

TRANSLATE one TracerouteResult (from src/measurement/traceroute.py) into
lines of text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Labels on every line: device, target

Lines produced:
    traceroute_status              0..3   always - see STATUS_CODES below
    traceroute_hops_reached        n      highest hop that replied (if it ran)
    traceroute_hop_responded{hop}  1 / 0  one line per hop (0 = "* * *")
    traceroute_hop_rtt_ms{hop, probe, hop_ip}   ms   one line per probe that
                                          replied - the time EXACTLY as printed
    traceroute_hop_annotation_info{hop, probe, hop_ip, annotation}   1
                                          only if traceroute printed e.g. "!X"
    traceroute_last_reply_info{hop, hop_ip}   1   the last router that replied:
                                          WHERE along the path replies stop
    traceroute_error_info{error_detail}  1  only if it could not run

Why hop_ip is on every probe (not once per hop):
  one hop can show several IPs (load balancing), e.g. the real hop 9:
  "172.253.73.109 20.063 ms 192.178.86.209 19.717 ms 8.8.8.8 20.138 ms"

The raw text (result.raw_output) is NOT sent: it is far too long for a
label. Every number in it is sent as its own line instead.
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# traceroute.py - we need its STATUS_* names for the table below.
from src.measurement import traceroute as tr_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


# ---------------------------------------------------------------------------
# STATUS TABLE
# ---------------------------------------------------------------------------

# Table that turns the status TEXT into a NUMBER for traceroute_status.
# In Grafana, "value mappings" turn the numbers back into these words.
STATUS_CODES = {
    tr_module.STATUS_REACHED: 0,        # a probe got a reply from the target
    tr_module.STATUS_NOT_REACHED: 1,    # ran, but the target never replied
    tr_module.STATUS_BLOCKED: 2,        # a router replied "blocked" (!X, !H, ...)
    tr_module.STATUS_COULD_NOT_RUN: 3,  # traceroute did not run or did not finish
}

# Safety net: a status not in the table is stored as -1 ("unknown").
UNKNOWN_STATUS_CODE = -1


# ---------------------------------------------------------------------------
# TRANSLATE
# ---------------------------------------------------------------------------

def to_lines(result: "tr_module.TracerouteResult") -> List[str]:
    """
    Turn one TracerouteResult into VictoriaMetrics lines.

    Example (real hop 6: "155.232.128.76  31.528 ms * *"):
        traceroute_hop_responded{device="pi5",target="8.8.8.8",hop="6"} 1 ...
        traceroute_hop_rtt_ms{...,hop="6",probe="1",hop_ip="155.232.128.76"} 31.528 ...
    (probes 2 and 3 got no reply, so they have no time line)
    """

    # The time, as milliseconds (the START of the run).
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line.
    labels = {"device": line_format.DEVICE, "target": result.target}

    # Look up the status in the table above to get its number.
    status = STATUS_CODES.get(result.status, UNKNOWN_STATUS_CODE)
    lines = [line_format.line("traceroute_status", labels, status, ts)]

    # Remember the last router that replied while going through the hops.
    last_hop, last_ip = None, None

    for hop in result.hops:
        hop_labels = dict(labels, hop=str(hop.hop))

        # Did ANY probe of this hop get a reply?
        replied = any(p.rtt_ms is not None for p in hop.probes)
        lines.append(line_format.line("traceroute_hop_responded", hop_labels, int(replied), ts))

        # One line per probe that replied. probe = 1, 2, 3 (order printed).
        for number, probe in enumerate(hop.probes, start=1):
            probe_labels = dict(hop_labels, probe=str(number), hop_ip=probe.ip or "")

            if probe.rtt_ms is not None:
                lines.append(line_format.line("traceroute_hop_rtt_ms", probe_labels, probe.rtt_ms, ts))
                last_hop, last_ip = hop.hop, probe.ip

            if probe.annotation:
                note_labels = dict(probe_labels, annotation=probe.annotation)
                lines.append(line_format.line("traceroute_hop_annotation_info", note_labels, 1, ts))

    # Highest hop that replied, and which router it was.
    if last_hop is not None:
        lines.append(line_format.line("traceroute_hops_reached", labels, last_hop, ts))
        last_labels = dict(labels, hop=str(last_hop), hop_ip=last_ip or "")
        lines.append(line_format.line("traceroute_last_reply_info", last_labels, 1, ts))

    # Error message, if it could not run.
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("traceroute_error_info", info_labels, 1, ts))

    return lines