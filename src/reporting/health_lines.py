"""
src/reporting/health_lines.py

TRANSLATE one SystemHealthResult (from src/measurement/system_health.py)
into lines of text that VictoriaMetrics understands.

It only translates. It does NOT send - victoria.py does that.

Rules:
  - every line gets the label device="pi5"
  - a value that is None (could not be read) is SKIPPED - None is not a number
  - True/False become 1/0
  - error text goes into a label (like ping_error_info); the value is just 1

Lines produced (example values from the real Pi reading):
    pi_input_voltage_v                          5.12
    pi_throttled_raw                            0
    pi_throttled{flag="under_voltage",when="now"}       0   (8 lines, one per flag)
    pi_cpu_temp_celsius                         60.5
    pi_cpu_freq_mhz                             2400
    pi_cpu_cores                                4
    pi_cpu_usage_pct                            3.2   (missing on the very first reading)
    pi_load_avg{period="1m"}                    0.5   (3 lines: 1m, 5m, 15m)
    pi_memory_total_bytes / _available_bytes / _used_pct
    pi_disk_total_bytes{mount="/"} / pi_disk_used_pct{mount="/"}
    pi_rootfs_writable                          1
    pi_uptime_seconds                           86400
    pi_clock_synced                             1
    pi_service_up{service="victoria"}           1     (one line per service)
    pi_health_error_info{reading="...",error_detail="..."}  1   (only if something failed)
"""

# These are only "type hints" - they do not change how the code runs.
from typing import List

# system_health.py - only needed for the type hint below.
from src.measurement import system_health as health_module

# Our helpers: to_ms(), line(), short_text() and DEVICE.
from src.reporting import line_format


def to_lines(result: "health_module.SystemHealthResult") -> List[str]:
    """Turn one SystemHealthResult into VictoriaMetrics lines."""

    # The time, as milliseconds. Every line from this reading gets the same time.
    ts = line_format.to_ms(result.timestamp)

    # The label that goes on every line.
    base = {"device": line_format.DEVICE}

    lines: List[str] = []

    def add(name: str, value, **extra_labels) -> None:
        """
        Add one line, unless the value is None.
        extra_labels are labels on top of device, e.g. add(..., period="1m").
        """
        if value is None:                 # could not be read -> skip
            return
        if isinstance(value, bool):       # True -> 1, False -> 0
            value = int(value)
        labels = dict(base, **extra_labels)
        lines.append(line_format.line(name, labels, value, ts))

    # --- Power and thermal --------------------------------------------------
    add("pi_input_voltage_v", result.input_voltage_v)
    add("pi_throttled_raw", result.throttled_raw)

    # throttle_flags keys look like "under_voltage_now" or "freq_capped_occurred".
    # rsplit("_", 1) cuts at the LAST "_":
    #   "under_voltage_now" -> flag="under_voltage", when="now"
    for key, is_set in result.throttle_flags.items():
        flag, when = key.rsplit("_", 1)
        add("pi_throttled", is_set, flag=flag, when=when)

    add("pi_cpu_temp_celsius", result.cpu_temp_c)
    add("pi_cpu_freq_mhz", result.cpu_freq_mhz)

    # --- Load ---------------------------------------------------------------
    add("pi_cpu_cores", result.cpu_cores)
    add("pi_cpu_usage_pct", result.cpu_usage_pct)
    add("pi_load_avg", result.load_1m, period="1m")
    add("pi_load_avg", result.load_5m, period="5m")
    add("pi_load_avg", result.load_15m, period="15m")

    # --- Memory and storage -------------------------------------------------
    add("pi_memory_total_bytes", result.mem_total_bytes)
    add("pi_memory_available_bytes", result.mem_available_bytes)
    add("pi_memory_used_pct", result.mem_used_pct)
    add("pi_disk_total_bytes", result.disk_total_bytes, mount="/")
    add("pi_disk_used_pct", result.disk_used_pct, mount="/")
    add("pi_rootfs_writable", result.rootfs_writable)

    # --- System -------------------------------------------------------------
    add("pi_uptime_seconds", result.uptime_s)
    add("pi_clock_synced", result.clock_synced)
    for service, is_up in result.services_up.items():
        add("pi_service_up", is_up, service=service)

    # --- Errors -------------------------------------------------------------
    # Each error looks like "input_voltage: vcgencmd not found".
    # partition(": ") splits at the first ": " ->
    #   reading="input_voltage", error_detail="vcgencmd not found"
    for error in result.errors:
        reading, _, detail = error.partition(": ")
        add("pi_health_error_info", 1,
            reading=reading, error_detail=line_format.short_text(detail))

    return lines