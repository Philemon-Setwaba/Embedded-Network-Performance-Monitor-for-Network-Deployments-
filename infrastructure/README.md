# Infrastructure

Install scripts, systemd service files and configuration for the four services
that run on the monitor (Raspberry Pi 5). Install them in this order, from the
repository root, as your normal user with `sudo`. The full steps, including the
prerequisites, are in the main [README](../README.md#installing-the-monitor-raspberry-pi).

| Order | Folder | Service | Port | What it does |
| --- | --- | --- | --- | --- |
| 1 | `victoria/` | `victoria` | 8428 | Local database (VictoriaMetrics v1.153.0), keeps 12 months of data |
| 2 | `vmagent/` | `vmagent` | 127.0.0.1:8429 | Store-and-forward to the remote receiver |
| 3 | `netmon/` | `netmon` | – | The monitor program (`python3 -m src.scheduler`) |
| 4 | `grafana/` | `grafana-server` | 3000 | Local dashboard |

```bash
sudo bash infrastructure/victoria/install.sh
sudo bash infrastructure/vmagent/install.sh
sudo bash infrastructure/netmon/install.sh
sudo bash infrastructure/grafana/install.sh
```

All scripts can safely be run again, for example after changing a service file.
Every service starts at boot and is restarted automatically if it stops.

## victoria/

| File | Contents |
| --- | --- |
| `install.sh` | Downloads VictoriaMetrics v1.153.0 for arm64, creates the `victoriametrics` user and the data folder `/var/lib/victoriametrics`, installs and starts the service |
| `victoria.service` | Runs VictoriaMetrics on port 8428 with 12 months of retention |
| `scrape.yml` | Reads vmagent's own metrics (buffer size, bytes sent, dropped data) every 15 s, so they appear on the dashboard |

## vmagent/

| File | Contents |
| --- | --- |
| `install.sh` | Installs the service; vmagent itself must already be at `/usr/local/bin/vmagent` |
| `vmagent.service` | Template filled in by `install.sh` from `config.json`. Disk buffer in `/var/lib/vmagent`, capped at 9 GB (about 12 months of data); sending rate capped at 62.5 kB/s (10 % of a 5 Mbit/s uplink) |
| `set_receiver.sh` | Links the name `netmon-receiver` to the receiver's address from `config.json`, in `/etc/hosts` and in the cloud-init template so it survives a reboot. Run it again only if the receiver changes |

vmagent sends to the name `netmon-receiver`, not to an IP address, because it
keeps a separate buffer for each address: a fixed name means data waiting in the
buffer is never left behind when the receiver's address changes.

## netmon/

| File | Contents |
| --- | --- |
| `install.sh` | Installs the service for the user who ran `sudo`, pointing at this repository |
| `netmon.service` | Runs `python3 -m src.scheduler` as a normal user (not root), starts after `victoria` and `vmagent`, restarts 10 s after a crash |

## grafana/

| File | Contents |
| --- | --- |
| `install.sh` | Copies the data source, the dashboard loader and the dashboard into Grafana and restarts it. Grafana itself must already be installed |
| `provisioning/datasource.yaml` | The VictoriaMetrics data source (`http://localhost:8428`) |
| `provisioning/dashboards.yaml` | Loads the dashboard into the folder *Network Monitor* |
| `dashboards/network-monitor.json` | The dashboard: eight tabs (Overview, Backhaul, Link use, Throughput, Traceroute, Wi-Fi, Pi health, Store-and-forward). Screenshots are in [`docs/dashboard/`](../docs/dashboard/) |

The same provisioning files set up Grafana on the receiver, where the dashboard
shows the receiver's copy of the data.

## Checking the services

```bash
systemctl is-active victoria vmagent netmon grafana-server
journalctl -u netmon -f
```