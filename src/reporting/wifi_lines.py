"""
src/reporting/wifi_lines.py

TRANSLATE one WifiResult (from src/measurement/wifi_signal.py) into lines
of text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Lines produced (labels on every line: device, interface):
    wifi_status            0 / 1 / 2  always  - see STATUS CODES below
    wifi_signal_dbm        -59        only if connected (closer to 0 = stronger)
    wifi_freq_mhz          5580.0     only if connected
    wifi_rx_bitrate_mbps   200.0      only if connected (radio rate, not real throughput)
    wifi_tx_bitrate_mbps   24.0       only if connected (radio rate, not real throughput)
    wifi_link_info         1          only if connected - text in labels ssid, bssid
    wifi_error_info        1          only if iw failed - text in label error_detail

Why ssid/bssid are on their OWN line (wifi_link_info):
  The bssid changes when the Pi moves to another access point. If it were a
  label on wifi_signal_dbm, the signal graph would break into a new line
  every time. On its own line, Grafana can still show WHEN it switched.
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# wifi_signal.py - only needed for the type hint below.
from src.measurement import wifi_signal as wifi_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


# ---------------------------------------------------------------------------
# STATUS CODES
# ---------------------------------------------------------------------------

# WifiResult has connected=False in TWO different cases:
#   - iw worked and said "Not connected."  -> there is no Wi-Fi
#   - iw itself failed (error_detail set)   -> we could not check
# These mean different things, so they get different numbers.
# In Grafana, "value mappings" turn the numbers back into words.
STATUS_CONNECTED = 0
STATUS_NOT_CONNECTED = 1
STATUS_ERROR = 2


# ---------------------------------------------------------------------------
# TRANSLATE
# ---------------------------------------------------------------------------

def to_lines(result: "wifi_module.WifiResult") -> List[str]:
    """
    Turn one WifiResult into VictoriaMetrics lines.

    Example input (real reading on the Pi):
        WifiResult(interface='wlan0', connected=True, ssid='eduroam',
                   bssid='74:11:b2:2b:c3:8e', freq_mhz=5580.0, signal_dbm=-59,
                   rx_bitrate_mbps=200.0, tx_bitrate_mbps=24.0, error_detail=None)
    Output:
        wifi_status{device="pi5",interface="wlan0"} 0 ...
        wifi_signal_dbm{...} -59 ...
        wifi_freq_mhz{...} 5580.0 ...
        wifi_rx_bitrate_mbps{...} 200.0 ...
        wifi_tx_bitrate_mbps{...} 24.0 ...
        wifi_link_info{...,ssid="eduroam",bssid="74:11:b2:2b:c3:8e"} 1 ...
    """

    # The time, as milliseconds. Every line from this reading gets the same time.
    ts = line_format.to_ms(result.timestamp)

    # The labels that go on every line.
    labels = {"device": line_format.DEVICE, "interface": result.interface}

    # Work out the status number (see STATUS CODES above).
    if result.error_detail:
        status = STATUS_ERROR
    elif result.connected:
        status = STATUS_CONNECTED
    else:
        status = STATUS_NOT_CONNECTED

    # This line is ALWAYS sent.
    lines = [line_format.line("wifi_status", labels, status, ts)]

    # The numbers - only sent if they were read (None is not a number).
    numbers = [
        ("wifi_signal_dbm", result.signal_dbm),
        ("wifi_freq_mhz", result.freq_mhz),
        ("wifi_rx_bitrate_mbps", result.rx_bitrate_mbps),
        ("wifi_tx_bitrate_mbps", result.tx_bitrate_mbps),
    ]
    for name, value in numbers:
        if value is not None:
            lines.append(line_format.line(name, labels, value, ts))

    # Which network and access point - text goes in labels, value is just 1.
    # "or ''" puts an empty label if iw did not print that field.
    if result.connected:
        info_labels = dict(labels, ssid=result.ssid or "", bssid=result.bssid or "")
        lines.append(line_format.line("wifi_link_info", info_labels, 1, ts))

    # Error message, if iw failed (like ping_error_info).
    if result.error_detail:
        info_labels = dict(labels, error_detail=line_format.short_text(result.error_detail))
        lines.append(line_format.line("wifi_error_info", info_labels, 1, ts))

    return lines