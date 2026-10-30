# Embedded Network Performance Monitor for Network Deployments

A low-cost network performance monitor for community networks, built on a
Raspberry Pi 5. It measures the backhaul and the access link, shows the results
on a local dashboard, keeps measuring when the backhaul fails, and delivers
everything it measured once the link returns.

Final-year project, EEE4022F, University of Cape Town (27 / 10 / 2026).
Author: Philemon Selaelo Setwaba. Supervisor: Dr Joyce Mwangama.

---

## Contents

1. [What the monitor does](#what-the-monitor-does)
2. [Architecture](#architecture)
3. [Hardware](#hardware)
4. [Repository structure](#repository-structure)
5. [Installing the monitor (Raspberry Pi)](#installing-the-monitor-raspberry-pi)
6. [Setting up the remote receiver (laptop or server)](#setting-up-the-remote-receiver-laptop-or-server)
7. [Running and checking the monitor](#running-and-checking-the-monitor)
8. [Tests and results](#tests-and-results)
9. [Enclosure](#enclosure)
10. [Known limitations](#known-limitations)

---

## What the monitor does

| Measurement | Tool | Lane | Interval |
| --- | --- | --- | --- |
| Latency, packet loss | `ping` (5 packets to 8.8.8.8) | Active | 10 s |
| Reachability when ping fails | TCP connect to 1.1.1.1:443 | Active | after a ping with no reply |
| Backhaul state (up / down) | ping + TCP check (down after 2 failed checks) | Active | after every ping |
| Network path | `traceroute` to 8.8.8.8 | Active | hourly, and when the backhaul goes down |
| Throughput, jitter, UDP loss | `iperf3` (TCP up, TCP down, UDP 500 kbit/s) | Active | one set per daily slot, only on a quiet link |
| Router traffic on the LTE link | SNMP (`lte0` counters) | Passive | 10 s |
| Wi-Fi signal and link rate | `iw` | Passive | 2 s |
| Pi health (CPU, RAM, temperature, voltage, throttling, SD card) | Linux, `vcgencmd` | Passive | 10 s |

A two-lane scheduler (`src/scheduler.py`) runs the active tests one at a time,
so they never overlap, while the passive readings run in a separate lane.

Every result is stored on the Pi in VictoriaMetrics and shown on a Grafana
dashboard that reads only the local database, so the dashboard works during an
outage. vmagent forwards every result to a remote VictoriaMetrics receiver over
Tailscale. While the receiver cannot be reached, vmagent keeps the data in a
9 GB disk buffer (about 12 months of data) and sends it, at no more than
62.5 kB/s (10 % of a 5 Mbit/s uplink), once the link returns.

## Architecture

```text
            Raspberry Pi 5 (monitor)                                  Remote receiver
 ┌──────────────────────────────────────────────┐                ┌──────────────────────┐
 │ netmon (src/scheduler.py)                    │                │ VictoriaMetrics :8428│
 │   passive lane: SNMP, Wi-Fi, health          │                │ Grafana (same        │
 │   active lane:  ping, TCP, state,            │                │ dashboard)           │
 │                 traceroute, iperf3           │                │ iperf3 server :5201  │
 │        │                     │               │                └──────────▲───────────┘
 │        ▼                     ▼               │                           │
 │ victoria :8428         vmagent :8429 ────────┼── LTE + Tailscale ────────┘
 │ (local database,       (disk buffer 9 GB,    │
 │  12 months)             62.5 kB/s limit)     │
 │        │                                     │
 │        ▼                                     │
 │ grafana-server :3000 (local dashboard,       │
 │  also on the 7" touch screen)                │
 └──────────────────────────────────────────────┘
```

The four parts run as systemd services (`victoria`, `vmagent`, `netmon`,
`grafana-server`) that start at boot and restart after a crash.

## Hardware

| Component | Unit price (R) |
| --- | --- |
| Raspberry Pi 5 (8 GB) | 4 249.00 |
| Raspberry Pi 7-inch Touch Display 2 | 1 159.90 |
| Mercusys MB135-4G AC1200 4G LTE router | 908.00 |
| SanDisk Ultra 64 GB microSDXC card | 380.00 |
| Raspberry Pi 27 W USB-C power supply (5.1 V, 5 A) | 349.00 |
| **Total** | **7 045.90** |

Prices from South African retailers, October 2026, including VAT.

Software versions used: 64-bit Raspberry Pi OS (Debian 13), Python 3 (standard
library only), VictoriaMetrics and vmagent v1.153.0, Grafana 13.2.3,
Tailscale 1.104.1, iperf 3.16 on the receiver.

## Repository structure

| Path | Contents |
| --- | --- |
| `config.json` | All site settings: targets, intervals, router, receiver |
| `src/scheduler.py` | The two-lane scheduler (started as `python3 -m src.scheduler`) |
| `src/jobs/` | One job per measurement, plus `state.py` (backhaul state logic) |
| `src/measurement/` | Wrappers around ping, TCP, traceroute, iperf3, SNMP, Wi-Fi and health |
| `src/reporting/` | Converts results to line protocol and pushes them to VictoriaMetrics and vmagent |
| `infrastructure/victoria/` | VictoriaMetrics install script and service |
| `infrastructure/vmagent/` | vmagent install script, service and `set_receiver.sh` |
| `infrastructure/netmon/` | Service and install script for the monitor program |
| `infrastructure/grafana/` | Dashboard (`network-monitor.json`), provisioning files and install script |
| `scripts/laptop_services.py` | Starts, stops and checks the receiver services (VictoriaMetrics, iperf3 server, Grafana); used for all tests and for normal running |
| `scripts/run_iperf_server.sh` | Plain iperf3 server, used only during early development |
| `scripts/` (other) | The timing and buffer-sizing scripts |
| `tests/` | One folder per test: the script that ran it, its logs and exports, the analysis notebook and the figures |
| `docs/dashboard/` | Screenshots of every dashboard tab |
| `hardware/enclosure/` | Fusion 360 design of the enclosure |

## Installing the monitor (Raspberry Pi)

Run these steps on a Raspberry Pi 5 with 64-bit Raspberry Pi OS, as your
normal user (not root).

**1. Install the system tools and get the code**

```bash
sudo apt update
sudo apt install -y git curl iperf3 snmp traceroute iw
git clone https://github.com/Philemon-Setwaba/Embedded-Network-Performance-Monitor-for-Network-Deployments-.git
cd Embedded-Network-Performance-Monitor-for-Network-Deployments-
```

**2. Enable SNMP on the router**

In the router's web page, enable SNMP v2c with the community `public` (or
change `snmp.community` in `config.json`). Check it from the Pi:

```bash
snmpwalk -v2c -c public 192.168.1.1 1.3.6.1.2.1.2.2.1.2
```

The LTE interface must appear in the list (`lte0` on the MB135-4G).

**3. Join the Pi and the receiver to Tailscale**

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install Tailscale on the receiver as well and note its address
(`tailscale ip -4` on the receiver).

**4. Edit `config.json`**

Set `remote.ip` to the receiver's Tailscale address. Change the router address,
the interface names or the targets only if your site differs.

**5. Install VictoriaMetrics (local database)**

```bash
sudo bash infrastructure/victoria/install.sh
```

**6. Install vmagent (store-and-forward)**

```bash
cd /tmp
curl -LO https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.153.0/vmutils-linux-arm64-v1.153.0.tar.gz
tar -xzf vmutils-linux-arm64-v1.153.0.tar.gz vmagent-prod
sudo install -m 755 vmagent-prod /usr/local/bin/vmagent
cd -
sudo bash infrastructure/vmagent/install.sh
```

`install.sh` runs `set_receiver.sh`, which links the name `netmon-receiver` to
the receiver's address in `/etc/hosts` and in the cloud-init template
(`/etc/cloud/templates/hosts.debian.tmpl`), so the entry survives a reboot.
vmagent always sends to the name, so its buffer is never lost when the
receiver's address changes. If the receiver is replaced, edit `remote.ip` and
run:

```bash
sudo bash infrastructure/vmagent/set_receiver.sh
```

**7. Install the monitor program**

```bash
sudo bash infrastructure/netmon/install.sh
```

**8. Install the dashboard**

Install Grafana OSS for arm64 (the `.deb` from grafana.com), then:

```bash
sudo bash infrastructure/grafana/install.sh
```

Open `http://<pi-address>:3000`. The dashboard is in the folder
*Network Monitor*.

## Setting up the remote receiver (laptop or server)

The receiver stores the forwarded data and runs the iperf3 server. In this
project it was a Windows 11 laptop with WSL (mirrored networking); any
always-on machine on the same Tailscale network will do.

1. **VictoriaMetrics.** Download VictoriaMetrics v1.153.0 for Windows from the
   [releases page](https://github.com/VictoriaMetrics/VictoriaMetrics/releases/tag/v1.153.0)
   and extract it to `C:\victoria`. It listens on port 8428, and vmagent sends
   to `http://netmon-receiver:8428/api/v1/write`.
2. **Grafana.** Install Grafana and copy the files from
   `infrastructure/grafana/provisioning/` into Grafana's
   `conf/provisioning/datasources/` and `conf/provisioning/dashboards/`
   folders. Set `options.path` in `dashboards.yaml` to the folder holding
   `network-monitor.json`, then restart Grafana. The same dashboard then shows
   the receiver's copy of the data.
3. **iperf3.** Install it in WSL: `sudo apt install iperf3`.
4. **Start, stop and check the receiver services** from WSL, in the project
   folder, with `scripts/laptop_services.py`:

   ```bash
   python3 scripts/laptop_services.py start    # VictoriaMetrics + iperf3 server
   python3 scripts/laptop_services.py status   # receiver, iperf3 and Grafana
   python3 scripts/laptop_services.py stop
   ```

   VictoriaMetrics runs in its own minimised window, so it keeps running when
   WSL closes. Each start of the iperf3 server (port 5201) writes a new
   numbered log to `~/iperf3-logs/`, which was used as a second record of every
   iperf3 test. The script keeps 31 days of data on the receiver
   (`-retentionPeriod=31d`), which was enough for testing; for a permanent
   receiver, set it to 12 months in `VM_ARGS`.
5. **Check the connection** from the Pi:

   ```bash
   curl -s http://netmon-receiver:8428/health
   ```

   It should print `OK`.

## Running and checking the monitor

```bash
# Are all four services running?
systemctl is-active victoria vmagent netmon grafana-server

# Live log of the monitor and of the forwarder
journalctl -u netmon -f
journalctl -u vmagent -f

# Data waiting in the buffer (0 when everything has been sent)
curl -s http://127.0.0.1:8429/metrics | grep '^vmagent_remotewrite_pending_data_bytes'

# Stop measuring (vmagent sends what is left before you stop it)
sudo systemctl stop netmon
sudo systemctl stop vmagent

# Start again
sudo systemctl start vmagent netmon
```

The monitor and its dashboard can also be reached remotely through
Raspberry Pi Connect.

## Tests and results

All tests ran on the real LTE backhaul (Mercusys MB135-4G, Vodacom). Each
folder holds the script that ran the test, its logs and database exports, the
analysis notebook and the figures used in the report.

| Folder | Test | What was done | Main result |
| --- | --- | --- | --- |
| `tests/test1_packet_loss/` | 1: Packet loss | NetEm loss 0–100 % in 10 min steps | Loss inside the 95 % interval at 7 of 8 levels; cut detected in 15.4 s; no sample lost |
| `tests/test2_delay/` | 2: Delay | NetEm delay 100–2500 ms and ±10/25/50 ms variation | Delay error −2.2 to +3.9 ms; no false outage at 2.5 s |
| `tests/test3_receiver_outage/` | 3: Receiver outage | Receiver off for 10 h | All 273,305 samples delivered; buffer lasts about 388 days |
| `tests/test4_snmp/` | 4: SNMP traffic | iperf3 upload and download through the LTE link | SNMP followed every transfer, 14–20 % above iperf3 |
| `tests/test5_backhaul_outage/` | 5: Backhaul outage | Mobile data off for 10 and 45 min | Detected in 16–17 s; all samples delivered |
| `tests/test6_power_cut/` | 6: Long run and power cut | Receiver off overnight, two outages, power cut | Services back 69 s after boot; buffer kept |
| `tests/test7_normal_operation/` | 7: Normal operation | 30.5 h unattended | No false alarm; passive jobs 100 % complete; CPU 0.6 % |

`tests/count.sh` compares the number of samples on the Pi and on the receiver
for a time window:

```bash
bash tests/count.sh 2026-10-16T09:00:00Z 2026-10-16T11:00:00Z
```

The full method and analysis are in the project report (Chapters 3 and 4).

## Enclosure

The monitor is housed in a two-part box designed in Autodesk Fusion 360: a
base that holds the Pi and the Touch Display 2, and a roof that clips on with
snap fits, so the box opens without tools. The full design, with all
dimensions, is in `hardware/enclosure/`.

## Known limitations

- LTE signal quality is not measured, because the router does not report it
  over SNMP.
- The backhaul state is either up or down; a reachable but very lossy link is
  reported as up.
- The Pi has no battery for its real-time clock, so after a power cut its
  timestamps are wrong until it reaches a time server.
- A power cut can lose the few samples that vmagent still holds in memory.
- iperf3 data use grows with the speed of the link, because each test is
  limited by time.
- The receiver and iperf3 server ran on a laptop, not a dedicated server.