"""
src/reporting/iperf_lines.py

TRANSLATE one IperfResult (from src/measurement/iperf.py) into lines of
text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Labels on every line: device, server, port, test, duration_s
  test = tcp_upload, tcp_download or udp. Because the test type is a label,
  all three tests share the same line names, so one Grafana query can show
  upload and download side by side.

Lines produced:
    iperf_success           1 / 0    always
    iperf_throughput_mbps   Mbit/s   if the test worked (all tests)
    iperf_retransmits       count    TCP only
    iperf_jitter_ms         ms       UDP only
    iperf_lost_packets      count    UDP only
    iperf_total_packets     count    UDP only
    iperf_lost_pct          %        UDP only
    iperf_error_info        1        only if it failed (message in label error_detail)

Why iperf_success and not a status number:
  IperfResult only says worked / failed, plus iperf3's error text.
  A status table would mean guessing error types from the text.
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# iperf.py - only needed for the type hint below.
from src.measurement import iperf as iperf_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


def to_lines(result: "iperf_module.IperfResult") -> List[str]:
    """
    Turn one IperfResult into VictoriaMetrics lines.

    Example input (a UDP test):
        IperfResult(test='udp', server='196.42.111.8', port=5201, duration_s=10,
                    success=True, throughput_mbps=0.5, jitter_ms=0.512,
                    lost_packets=3, total_packets=432, lost_pct=0.69)
    Output:
        iperf_success{device="pi5",server="196.42.111.8",port="5201",test="udp",duration_s="10"} 1 ...
        iperf_throughput_mbps{...} 0.5 ...
        iperf_jitter_ms{...} 0.512 ...
        iperf_lost_packets{...} 3 ...
        iperf_total_packets{...} 432 ...
        iperf_lost_pct{...} 0.69 ...
    """

    # The time, as milliseconds (the START of the test).
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line.
    # Numbers are turned into text because labels are always text.
    labels = {
        "device": line_format.DEVICE,
        "server": result.server,
        "port": str(result.port),
        "test": result.test,
        "duration_s": str(result.duration_s),
    }

    # This line is ALWAYS sent.
    lines = [line_format.line("iperf_success", labels, int(result.success), ts)]  # True -> 1

    # The numbers - only sent if they exist (None is not a number).
    # TCP tests have retransmits; UDP tests have jitter and loss.
    numbers = [
        ("iperf_throughput_mbps", result.throughput_mbps),
        ("iperf_retransmits", result.retransmits),
        ("iperf_jitter_ms", result.jitter_ms),
        ("iperf_lost_packets", result.lost_packets),
        ("iperf_total_packets", result.total_packets),
        ("iperf_lost_pct", result.lost_pct),
    ]
    for name, value in numbers:
        if value is not None:
            lines.append(line_format.line(name, labels, value, ts))

    # Only send iperf_error_info when there is an error message.
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("iperf_error_info", info_labels, 1, ts))

    return lines