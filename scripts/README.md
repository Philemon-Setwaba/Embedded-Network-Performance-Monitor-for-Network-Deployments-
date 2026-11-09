# Scripts

Scripts that run on the **receiver** (the laptop or server), not on the Pi.
Run them from the repository root.

| Script | Use |
| --- | --- |
| `laptop_services.py` | Starts, stops and checks the receiver services. Used for all seven tests and for normal running |
| `run_iperf_server.sh` | Plain iperf3 server on port 5201. Used only during early development; `laptop_services.py` replaced it |

## laptop_services.py

Runs in WSL on the Windows laptop and controls three services:

| Service | Where | Port |
| --- | --- | --- |
| VictoriaMetrics (the receiver) | Windows, `C:\victoria` | 8428 |
| iperf3 server | WSL | 5201 |
| Grafana | Windows service (only checked, not started) | 3000 |

```bash
python3 scripts/laptop_services.py start    # start VictoriaMetrics and the iperf3 server
python3 scripts/laptop_services.py status   # show whether all three are running
python3 scripts/laptop_services.py stop     # stop the iperf3 server and VictoriaMetrics
```

- VictoriaMetrics starts in its own minimised Windows window, so it keeps
  running when WSL closes. `stop` ends it with a hard stop; for a clean stop,
  press Ctrl+C in its window instead.
- Each start of the iperf3 server writes a new numbered log to
  `~/iperf3-logs/`, for example `test-003_2026-10-22_142416Z.log`. These logs
  were the second record of every iperf3 test, and are kept in the test
  folders under `tests/`.
- The receiver keeps 31 days of data (`-retentionPeriod=31d` in `VM_ARGS`).
  For a permanent receiver, set it to 12 months.
- The paths at the top of the script (`VM_EXE`, `VM_ARGS`) match the laptop
  used in this project. Change them if VictoriaMetrics is installed elsewhere.

## run_iperf_server.sh

```bash
bash scripts/run_iperf_server.sh
```

Starts an iperf3 server on port 5201 in the foreground; stop it with Ctrl+C.
It writes no log, which is why `laptop_services.py` was used for testing.