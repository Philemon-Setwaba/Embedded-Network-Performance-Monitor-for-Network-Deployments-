#!/usr/bin/env python3
"""
scripts/laptop_services.py

Start, stop and check the laptop side of the test setup, from WSL:

    receiver   VictoriaMetrics for Windows (C:\\victoria), port 8428
               - receives the data vmagent on the Pi forwards
    iperf3     iperf3 server in WSL, port 5201
               - the server the Pi's iperf3 tests run against
               - every start writes a NEW log file, numbered and dated:
                   ~/iperf3-logs/test-000_2026-10-18_190512Z.log
                   ~/iperf3-logs/test-001_2026-10-19_073000Z.log ...

Usage (in WSL, from the project folder):
    python3 scripts/laptop_services.py start
    python3 scripts/laptop_services.py status
    python3 scripts/laptop_services.py stop

Notes:
  - The receiver is started in its own minimised Windows console window,
    so it keeps running when this script (or the WSL window) ends.
  - "stop" ends the receiver with Stop-Process (a hard stop). For a clean
    stop, press Ctrl+C in the receiver's own window instead.
  - Grafana on the laptop is a Windows service; it is only checked here.
  - WSL uses mirrored networking, so "localhost" here is the laptop.
"""

import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# SETTINGS (Windows paths of the receiver, as set up on 2026-10-13)
# ---------------------------------------------------------------------------

VM_EXE = r"C:\victoria\victoria-metrics-windows-amd64-prod.exe"
VM_PROCESS = "victoria-metrics-windows-amd64-prod"   # process name, no .exe
VM_ARGS = [
    r"-storageDataPath=C:\victoria\data",
    "-retentionPeriod=31d",
    "-httpListenAddr=:8428",
]
VM_HEALTH = "http://localhost:8428/health"
GRAFANA_HEALTH = "http://localhost:3000/api/health"

IPERF_PORT = 5201
IPERF_LOG_DIR = Path.home() / "iperf3-logs"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def answers(url: str) -> bool:
    """True if the URL answers with HTTP 200 within 3 s."""
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def powershell(command: str) -> subprocess.CompletedProcess:
    """Run one PowerShell command on Windows (from WSL)."""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True,
    )


def next_iperf_log() -> Path:
    """
    A new log file: the next test number (000, 001, ...) and the UTC date
    and time, e.g. test-003_2026-10-18_190512Z.log.
    """
    IPERF_LOG_DIR.mkdir(exist_ok=True)
    numbers = []
    for f in IPERF_LOG_DIR.glob("test-*_*.log"):
        try:
            numbers.append(int(f.name[5:8]))
        except ValueError:
            pass
    number = max(numbers) + 1 if numbers else 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    return IPERF_LOG_DIR / f"test-{number:03d}_{stamp}.log"


def latest_iperf_log():
    """The newest log file, or None."""
    logs = sorted(IPERF_LOG_DIR.glob("test-*_*.log")) if IPERF_LOG_DIR.exists() else []
    return logs[-1] if logs else None


def iperf_supports_timestamps() -> bool:
    """True if this iperf3 has --timestamps (time at the start of each log line)."""
    r = subprocess.run(["iperf3", "--help"], capture_output=True, text=True)
    return "--timestamps" in (r.stdout + r.stderr)


def iperf_running() -> bool:
    """True if an iperf3 server is running in WSL."""
    return subprocess.run(["pgrep", "-f", f"iperf3 -s -p {IPERF_PORT}"],
                          capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# RECEIVER (VictoriaMetrics for Windows)
# ---------------------------------------------------------------------------

def start_receiver():
    if answers(VM_HEALTH):
        print("receiver : already running (port 8428)")
        return
    args = ",".join(f"'{a}'" for a in VM_ARGS)
    r = powershell(f"Start-Process -FilePath '{VM_EXE}' "
                   f"-ArgumentList {args} -WindowStyle Minimized")
    if r.returncode != 0:
        print("receiver : could not start:", (r.stderr or r.stdout).strip())
        return
    for _ in range(20):                       # wait up to 20 s
        time.sleep(1)
        if answers(VM_HEALTH):
            print("receiver : started (port 8428)")
            return
    print("receiver : started, but /health does not answer yet - run 'status'")


def stop_receiver():
    if not answers(VM_HEALTH):
        print("receiver : not running")
        return
    r = powershell(f"Stop-Process -Name '{VM_PROCESS}'")
    if r.returncode != 0:
        print("receiver : could not stop:", (r.stderr or r.stdout).strip())
        return
    time.sleep(2)
    print("receiver : stopped" if not answers(VM_HEALTH)
          else "receiver : still answering - check its window")


# ---------------------------------------------------------------------------
# IPERF3 SERVER (WSL)
# ---------------------------------------------------------------------------

def start_iperf():
    if iperf_running():
        print(f"iperf3   : already running (port {IPERF_PORT})")
        return
    if shutil.which("iperf3") is None:
        print("iperf3   : not installed in WSL (sudo apt install iperf3)")
        return
    log = next_iperf_log()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.write_text(f"# {log.stem}: iperf3 server started {started} (UTC), "
                   f"port {IPERF_PORT}\n")

    # -D = run in the background; output is added to the new log file.
    command = ["iperf3", "-s", "-p", str(IPERF_PORT), "-D", "--logfile", str(log)]
    if iperf_supports_timestamps():
        command.append("--timestamps")
    subprocess.run(command)
    time.sleep(1)
    print(f"iperf3   : started (port {IPERF_PORT})\n           log {log}"
          if iperf_running() else f"iperf3   : did not start - see {log}")


def stop_iperf():
    if not iperf_running():
        print("iperf3   : not running")
        return
    subprocess.run(["pkill", "-f", f"iperf3 -s -p {IPERF_PORT}"])
    time.sleep(1)
    log = latest_iperf_log()
    if log is not None and not iperf_running():
        stopped = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(log, "a") as f:
            f.write(f"# iperf3 server stopped {stopped} (UTC)\n")
    print("iperf3   : stopped" if not iperf_running() else "iperf3   : still running")


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def status():
    print("receiver :", "running (port 8428)" if answers(VM_HEALTH) else "NOT running")
    print("iperf3   :", f"running (port {IPERF_PORT})" if iperf_running() else "NOT running")
    log = latest_iperf_log()
    if log is not None:
        print("           latest log", log)
    print("grafana  :", "running (port 3000)" if answers(GRAFANA_HEALTH) else "NOT answering")


# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop", "status"):
        print("Usage: python3 scripts/laptop_services.py start|stop|status")
        sys.exit(1)

    action = sys.argv[1]
    if action == "start":
        start_receiver()
        start_iperf()
    elif action == "stop":
        stop_iperf()
        stop_receiver()
    print()
    status()


if __name__ == "__main__":
    main()