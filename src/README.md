# Source code

The monitor program. It runs on the Pi as the `netmon` service
(`python3 -m src.scheduler`, from the repository root) and uses only the Python
standard library. All site settings are read from [`config.json`](../config.json)
at start.

## How it works

```text
scheduler.py  ──decides WHEN──►  jobs/*_job.py  ──►  measurement/*.py   (run the tool, read the result)
                                        │
                                        ├──►  reporting/*_lines.py       (turn the result into metric lines)
                                        │
                                        └──►  reporting/victoria.py      (send the lines)
                                                   ├─► local VictoriaMetrics  (localhost:8428)
                                                   └─► vmagent                (127.0.0.1:8429, store-and-forward)
```

Each job measures, translates, sends, and keeps its latest result so other jobs
can read it. Every result is sent twice: to the local database and to vmagent.
The two sends are independent, so a vmagent failure never stops local storage.

## The scheduler (`scheduler.py`)

The scheduler only decides when each job runs. It runs two lanes, each in its
own thread:

| Lane | Jobs | Why |
| --- | --- | --- |
| Passive | SNMP (10 s), health (10 s), Wi-Fi (2 s) | Only read local values, so they keep their timing while a long active test runs |
| Active | ping (10 s), TCP check (after a ping with no reply), backhaul state (after each ping), traceroute (hourly, and when the backhaul goes down), iperf3 (one set per daily slot, only on a quiet link) | Send traffic over the link, so they run one at a time and never overlap |

Intervals are measured with `time.monotonic()`, so a clock correction (for
example after a power cut) does not make jobs run early or late.

## Folders

| Folder | Contents |
| --- | --- |
| `jobs/` | One job per measurement (`ping_job.py`, `tcp_job.py`, `traceroute_job.py`, `iperf_job.py`, `snmp_job.py`, `wifi_job.py`, `health_job.py`), the backhaul state logic (`state.py`) and `job_config.py`, which reads each job's interval from `config.json` |
| `measurement/` | Wrappers around the tools: `ping.py`, `tcp_reachability.py` (TCP connect to port 443), `traceroute.py`, `iperf.py`, `snmp.py` (`snmpget`/`snmpwalk`), `wifi_signal.py` (`iw`) and `system_health.py` (Linux and `vcgencmd`) |
| `reporting/` | One `*_lines.py` file per measurement, which turns a result into metric lines; `line_format.py`, with the shared helpers; and `victoria.py`, which sends the lines |

## Backhaul state (`jobs/state.py`)

After every ping, the state job reads the latest ping, TCP and Wi-Fi results and
stores `monitor_backhaul_state`:

| Value | State | When |
| --- | --- | --- |
| 0 | Backhaul up | Ping got at least one reply, or the TCP check got through |
| 1 | Backhaul down | Ping got no reply and the TCP check failed, two checks in a row |
| 2 | Local link down | The Pi's Wi-Fi is not connected, or ping reports "Network is unreachable" |
| −1 | Unknown | At start-up, until the first check |

One good check is enough to return to "up". `monitor_backhaul_fail_streak`
shows the number of failed checks in a row, so a first failure is visible
before it is confirmed.

## Metric format

Each line has a name, labels, a value and a timestamp in milliseconds, for
example:

```text
ping_rtt_ms{device="pi5",target="8.8.8.8",count="5"} 19.2 1791216953458
```

A value that could not be read is left out rather than sent as zero.