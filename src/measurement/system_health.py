"""
src/measurement/system_health.py

Measures the health of the Raspberry Pi itself, so a bad network result
can be told apart from a struggling monitor.

Why each reading matters (see Sierra-Herrera et al., Computers 2026,
15, 636 - Raspberry Pi 5 under undervoltage):
  - input voltage + under-voltage flags : power problems caused errors
                                           BEFORE performance dropped
  - CPU frequency                        : the CPU slowed to 1000 MHz at low voltage
  - CPU temperature                      : rose as the supply voltage fell
  - root filesystem writable             : SD card corruption was seen under power stress
  - uptime                               : a drop to ~0 means the Pi rebooted
  - CPU / load / memory / disk           : an overloaded Pi gives late or wrong measurements
  - clock synced                         : a wrong clock makes every timestamp wrong
  - services                             : is VictoriaMetrics / Grafana / snmpd running?

Everything is read from /proc, /sys or small system commands, so no
extra Python package is needed.

Usage (from the project folder):
    python3 src/measurement/system_health.py
"""

import dataclasses
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

# Services to check with `systemctl is-active`.
# "netmon" is the scheduler service; it shows as not running until we
# create that service later.
DEFAULT_SERVICES = ["victoria", "grafana-server", "snmpd", "netmon"]

# Safety limit for each small system command.
COMMAND_TIMEOUT_S = 3.0

# Files Linux keeps the readings in.
TEMP_FILE = "/sys/class/thermal/thermal_zone0/temp"            # millidegrees C
FREQ_FILE = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"  # kHz
STAT_FILE = "/proc/stat"
LOADAVG_FILE = "/proc/loadavg"
MEMINFO_FILE = "/proc/meminfo"
UPTIME_FILE = "/proc/uptime"
MOUNTS_FILE = "/proc/mounts"

# Bits of `vcgencmd get_throttled` (Raspberry Pi documentation).
THROTTLE_BITS = {
    "under_voltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "soft_temp_limit_now": 3,
    "under_voltage_occurred": 16,
    "freq_capped_occurred": 17,
    "throttled_occurred": 18,
    "soft_temp_limit_occurred": 19,
}

# Example output: "EXT5V_V volt(24)=5.09066000V"
INPUT_VOLTAGE_PATTERN = re.compile(r"EXT5V_V volt\(\d+\)=([\d.]+)V")


@dataclasses.dataclass
class SystemHealthResult:
    """One reading of the Pi's health. A value is None if it could not be read."""

    timestamp: str

    # Power and thermal
    input_voltage_v: Optional[float] = None
    throttled_raw: Optional[int] = None              # e.g. 0 = all clear
    throttle_flags: Dict[str, bool] = dataclasses.field(default_factory=dict)
    cpu_temp_c: Optional[float] = None
    cpu_freq_mhz: Optional[float] = None

    # Load
    cpu_cores: Optional[int] = None
    cpu_usage_pct: Optional[float] = None            # None on the very first reading
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    load_15m: Optional[float] = None

    # Memory and storage
    mem_total_bytes: Optional[int] = None
    mem_available_bytes: Optional[int] = None
    mem_used_pct: Optional[float] = None
    disk_total_bytes: Optional[int] = None
    disk_used_pct: Optional[float] = None
    rootfs_writable: Optional[bool] = None

    # System
    uptime_s: Optional[float] = None
    clock_synced: Optional[bool] = None
    services_up: Dict[str, bool] = dataclasses.field(default_factory=dict)

    # Short notes for anything that could not be read, e.g.
    # "input_voltage: vcgencmd not found"
    errors: List[str] = dataclasses.field(default_factory=list)


class SystemHealthMonitor:
    """
    Reads the Pi's health.

    One instance is kept for the whole run, because CPU usage is worked out
    from the DIFFERENCE between two readings of /proc/stat (like SNMP bps).
    """

    def __init__(self, services: Optional[List[str]] = None):
        self.services = services if services is not None else DEFAULT_SERVICES
        # Previous (busy, total) CPU time, for the CPU usage calculation.
        self._last_cpu_times: Optional[Tuple[int, int]] = None

    # -----------------------------------------------------------------------
    # One reading
    # -----------------------------------------------------------------------

    def read_once(self) -> SystemHealthResult:
        """Take one reading of everything. Never raises."""

        result = SystemHealthResult(timestamp=datetime.now(timezone.utc).isoformat())

        # Each step is separate: if one fails, the others still run.
        self._step(result, "input_voltage", self._read_input_voltage)
        self._step(result, "throttled", self._read_throttled)
        self._step(result, "cpu_temp", self._read_cpu_temp)
        self._step(result, "cpu_freq", self._read_cpu_freq)
        self._step(result, "cpu_usage", self._read_cpu_usage)
        self._step(result, "load", self._read_load)
        self._step(result, "memory", self._read_memory)
        self._step(result, "disk", self._read_disk)
        self._step(result, "rootfs", self._read_rootfs_writable)
        self._step(result, "uptime", self._read_uptime)
        self._step(result, "clock", self._read_clock_synced)
        self._step(result, "services", self._read_services)

        return result

    @staticmethod
    def _step(result: SystemHealthResult, name: str, func) -> None:
        """Run one reading; if it fails, note why and carry on."""
        try:
            func(result)
        except Exception as exc:  # any failure is recorded, not raised
            result.errors.append(f"{name}: {exc}")

    # -----------------------------------------------------------------------
    # Power and thermal
    # -----------------------------------------------------------------------

    def _read_input_voltage(self, result: SystemHealthResult) -> None:
        # Pi 5 only: the 5 V input measured by the power chip (PMIC).
        output = _run(["vcgencmd", "pmic_read_adc", "EXT5V_V"])
        match = INPUT_VOLTAGE_PATTERN.search(output)
        if not match:
            raise ValueError(f"unexpected output: {output.strip()!r}")
        result.input_voltage_v = round(float(match.group(1)), 3)

    def _read_throttled(self, result: SystemHealthResult) -> None:
        # Output looks like "throttled=0x0". Turn the hex number into bits.
        output = _run(["vcgencmd", "get_throttled"]).strip()
        value = int(output.split("=", 1)[1], 16)
        result.throttled_raw = value
        result.throttle_flags = {
            name: bool(value & (1 << bit)) for name, bit in THROTTLE_BITS.items()
        }

    def _read_cpu_temp(self, result: SystemHealthResult) -> None:
        # File holds millidegrees, e.g. 57300 -> 57.3 C
        result.cpu_temp_c = round(int(_read_file(TEMP_FILE)) / 1000, 1)

    def _read_cpu_freq(self, result: SystemHealthResult) -> None:
        # File holds kHz, e.g. 1500000 -> 1500 MHz
        result.cpu_freq_mhz = round(int(_read_file(FREQ_FILE)) / 1000, 0)

    # -----------------------------------------------------------------------
    # Load
    # -----------------------------------------------------------------------

    def _read_cpu_usage(self, result: SystemHealthResult) -> None:
        result.cpu_cores = os.cpu_count()

        # First line of /proc/stat: "cpu  user nice system idle iowait irq softirq steal ..."
        # Numbers are time units the CPUs spent in each state since boot.
        fields = [int(x) for x in _read_file(STAT_FILE).splitlines()[0].split()[1:9]]
        idle = fields[3] + fields[4]          # idle + iowait
        total = sum(fields)
        busy = total - idle

        # Usage = share of the time since the LAST reading that was busy.
        if self._last_cpu_times is not None:
            last_busy, last_total = self._last_cpu_times
            delta_total = total - last_total
            if delta_total > 0:
                result.cpu_usage_pct = round(100 * (busy - last_busy) / delta_total, 1)

        self._last_cpu_times = (busy, total)

    def _read_load(self, result: SystemHealthResult) -> None:
        # e.g. "0.51 0.37 0.22 2/632 5903" -> first three numbers
        parts = _read_file(LOADAVG_FILE).split()
        result.load_1m, result.load_5m, result.load_15m = (float(p) for p in parts[:3])

    # -----------------------------------------------------------------------
    # Memory and storage
    # -----------------------------------------------------------------------

    def _read_memory(self, result: SystemHealthResult) -> None:
        # Lines like "MemAvailable:  6123456 kB"
        info = {}
        for line in _read_file(MEMINFO_FILE).splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024     # kB -> bytes
        total = info["MemTotal"]
        available = info["MemAvailable"]
        result.mem_total_bytes = total
        result.mem_available_bytes = available
        result.mem_used_pct = round(100 * (total - available) / total, 1)

    def _read_disk(self, result: SystemHealthResult) -> None:
        # Same numbers `df /` uses. f_bavail = space a normal user can still use.
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free_for_users = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        result.disk_total_bytes = total
        # Matches df's "Use%": used / (used + space available to users)
        result.disk_used_pct = round(100 * used / (used + free_for_users), 1)

    def _read_rootfs_writable(self, result: SystemHealthResult) -> None:
        # Line like "/dev/mmcblk0p2 / ext4 rw,noatime 0 0".
        # If Linux finds SD card errors it can remount "/" read-only ("ro").
        for line in _read_file(MOUNTS_FILE).splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "/":
                result.rootfs_writable = "rw" in parts[3].split(",")
                return
        raise ValueError("root filesystem not found in /proc/mounts")

    # -----------------------------------------------------------------------
    # System
    # -----------------------------------------------------------------------

    def _read_uptime(self, result: SystemHealthResult) -> None:
        # First number = seconds since boot
        result.uptime_s = float(_read_file(UPTIME_FILE).split()[0])

    def _read_clock_synced(self, result: SystemHealthResult) -> None:
        # Output: "NTPSynchronized=yes"
        output = _run(["timedatectl", "show", "-p", "NTPSynchronized"]).strip()
        result.clock_synced = output.endswith("=yes")

    def _read_services(self, result: SystemHealthResult) -> None:
        # `systemctl is-active a b c` prints one word per service
        # ("active", "inactive", "failed", ...). Exit code is not 0 if any
        # is not active, so we do not treat that as an error.
        proc = subprocess.run(
            ["systemctl", "is-active", *self.services],
            capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S,
        )
        states = proc.stdout.split()
        result.services_up = {
            name: (state == "active") for name, state in zip(self.services, states)
        }


# ---------------------------------------------------------------------------
# SMALL HELPERS
# ---------------------------------------------------------------------------

def _read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def _run(command: List[str]) -> str:
    """Run a small command and return its output. Raises if it fails."""
    proc = subprocess.run(command, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"exit code {proc.returncode}")
    return proc.stdout


if __name__ == "__main__":
    # Two readings 2 s apart, so the second one has CPU usage.
    monitor = SystemHealthMonitor()
    print(monitor.read_once())
    time.sleep(2)
    print()
    print(monitor.read_once())