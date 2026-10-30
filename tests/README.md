# Tests

The seven tests of the project report (Chapter 3, Section 3.5, and Chapter 4).
All tests ran on the real LTE backhaul (Mercusys MB135-4G, Vodacom), with the
receiver and iperf3 server on the laptop, started with
`scripts/laptop_services.py`.

| Folder | Report | How it was run | Data folder | Notebook |
| --- | --- | --- | --- | --- |
| `test1_packet_loss/` | Test 1, Section 4.2 | `test1_loss.sh` | files in the folder itself | `test1_analysis.ipynb` |
| `test2_delay/` | Test 2, Section 4.3 | `test2_delay.sh` | `T2/` | `test2_analysis.ipynb` |
| `test3_receiver_outage/` | Test 3, Section 4.4 | `test3_receiver_outage.sh` | `T3/` | `test3_analysis.ipynb` |
| `test4_snmp/` | Test 4, Section 4.5 | `test4_snmp_validation.sh` with the laptop as server | `T4b/` | `test4_analysis.ipynb` |
| `test5_backhaul_outage/` | Test 5, Section 4.6 | `test5_backhaul_outage.sh` | `T6/` | `test5_analysis.ipynb` |
| `test6_power_cut/` | Test 6, Section 4.7 | By hand, times logged in `events.txt` | `T7/` | `test6_analysis.ipynb` |
| `test7_normal_operation/` | Test 7, Section 4.8 | By hand: the monitor was left running for 30.5 h | `T8/` | `test7_analysis.ipynb` |

## Folder names

The data folders keep the names they had during testing (`T1`–`T8`). Test
numbers changed when the report was written, so the data of Test 5 is in `T6/`,
Test 6 in `T7/` and Test 7 in `T8/`. There is no `T5`.
`test4_snmp/first_attempt/` holds an earlier run of Test 4 that is not used in
the report.

## Files in each test

| File | Contents |
| --- | --- |
| `export_pi5_127.0.0.1.jsonl.gz` | Every sample in the Pi's local database |
| `export_pi5_netmon-receiver.jsonl.gz` | Every sample in the receiver's database |
| `export_vmagent_pi.jsonl.gz` | vmagent's own metrics (buffer size, bytes sent, drops) |
| `journal.txt(.gz)` | systemd log of the monitor's services during the test |
| `test*_<UTC time>.csv` | The test script's own log: start and end of every step |
| `test-0NN_<date>.log` | iperf3 server log on the laptop (second record of every iperf3 test) |
| `laptop_t*.tar.gz` | Copy of the receiver's data taken on the laptop |
| `T*.tar.gz` | The data folder as packed on the Pi, with its checksum |
| `figures/`, `*.pdf`, `*.png` | Figures used in the report |

In Test 7, the two database exports are `T8/pi_db.jsonl.gz` and
`T8/receiver_db.jsonl.gz` (compressed to stay under GitHub's file size limit).

## Running a test again

Run each script on the Pi, from the repository root, in a terminal that stays
open (not over SSH or Tailscale, since some tests cut the link):

```bash
bash tests/test1_packet_loss/test1_loss.sh
bash tests/test2_delay/test2_delay.sh
bash tests/test3_receiver_outage/test3_receiver_outage.sh
SERVER=netmon-receiver PORT=5202 bash tests/test4_snmp/test4_snmp_validation.sh
bash tests/test5_backhaul_outage/test5_backhaul_outage.sh
```

Each script explains its steps and options at the top, including a short trial
run. Tests 6 and 7 have no script: follow the steps in Sections 4.7 and 4.8 of
the report.

To check that every sample reached the receiver, compare the two databases for
a time window:

```bash
bash tests/count.sh 2026-10-16T09:00:00Z 2026-10-16T11:00:00Z
```