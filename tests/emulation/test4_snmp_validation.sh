#!/bin/bash
# tests/emulation/test4_snmp_validation.sh
# Test 4: SNMP validation — do the router's lte0 SNMP counters track real LTE traffic?
# netmon (and its SNMP poller) runs throughout; iperf3 sends known TCP traffic to a
# public server (NOT netmon-receiver; config.json is not touched) while VictoriaMetrics
# records in_bps and out_bps on lte0. After the test, query Victoria for the SNMP
# time series and compare the spikes with the iperf3 TX/RX values logged here.
# Two identical runs back to back (~19 min total):
#   phase 0  quiet baseline     2 min   netmon running, no iperf3  (noise floor)
#   phase 1  TCP upload         2 min   iperf3 Pi -> server        (expect out_bps spike)
#   phase 2  settle             1 min   no iperf3
#   phase 3  TCP download (-R)  2 min   iperf3 server -> Pi        (expect in_bps spike)
#   phase 4  post-test          2 min (run 1) / 3 min (run 2)
# victoria and grafana stay up after the test so the SNMP data can be queried immediately.
#
# Run on the Pi, from the repository root:
#   bash tests/emulation/test4_snmp_validation.sh
#   SERVER=bouygues.testdebit.info bash tests/emulation/test4_snmp_validation.sh
# Quick trial (1 min phases):  PHASE_MIN=1 bash tests/emulation/test4_snmp_validation.sh

set -u
SERVER=${SERVER:-iperf.he.net}
PORT=${PORT:-5201}
PHASE_MIN=${PHASE_MIN:-2}             # minutes for phases 0, 1, 3 (phase 2 is always 1 min)
LOGDIR=$HOME/tests
mkdir -p "$LOGDIR"
LOG=$LOGDIR/test4_snmp_validation_$(date -u +%Y%m%dT%H%MZ).csv
now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
to_s() { awk -v m="$1" 'BEGIN { printf "%d", m * 60 }'; }    # minutes -> seconds

row() {    # phase, iperf_tx_mbps, iperf_rx_mbps, note
  echo "$1,$(now),$2,$3,$4" >> "$LOG"
}

iperf_run() {    # iperf_run phase upload|download seconds
  local flag="" col="sum_sent"
  [ "$2" = download ] && { flag=-R; col="sum_received"; }
  local out mbps
  out=$(iperf3 -c "$SERVER" -p $PORT -t "$3" $flag --json 2>&1) || true
  mbps=$(printf '%s' "$out" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    bps = d['end']['${col}']['bits_per_second']
    print(f'{bps / 1e6:.2f}')
except Exception:
    print('-')
" 2>/dev/null) || mbps="-"
  local tx="" rx=""
  [ "$2" = upload ]   && tx=$mbps
  [ "$2" = download ] && rx=$mbps
  row "$1" "$tx" "$rx" "iperf3 $2 ${mbps} Mbit/s"
  echo "    iperf3 $2: ${mbps} Mbit/s"
}

cleanup() {
  trap - EXIT INT TERM
  sudo systemctl stop netmon
  row cleanup "" "" "systemctl stop netmon"
  sudo systemctl stop vmagent
  row cleanup "" "" "systemctl stop vmagent"
  echo "Done. Log: $LOG"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

echo "phase,time_utc,iperf_tx_mbps,iperf_rx_mbps,note" > "$LOG"
sudo -v || exit 1

# ---- checks --------------------------------------------------------------
command -v iperf3  > /dev/null || { echo "iperf3 not installed."; exit 1; }
command -v python3 > /dev/null || { echo "python3 not installed."; exit 1; }
[ -f src/measurement/snmp.py ] \
  || { echo "Run from the repository root (src/measurement/snmp.py not found)."; exit 1; }
for s in netmon vmagent; do
  systemctl is-active --quiet $s \
    && { echo "$s is already running — stop it first."; exit 1; }
done
timeout 5 bash -c "exec 3<>/dev/tcp/$SERVER/$PORT" 2>/dev/null \
  || { echo "Cannot reach $SERVER:$PORT — check LTE connection."; exit 1; }

sudo systemctl start vmagent; sleep 5
row start "" "" "systemctl start vmagent"
sudo systemctl start netmon; sleep 2
row start "" "" "systemctl start netmon — test begins  server=$SERVER:$PORT"
MAIN_S=$(to_s "$PHASE_MIN")
echo "Test 4 running (~19 min). Log: $LOG"

# ---- two runs ------------------------------------------------------------
for run in 1 2; do
  LAST_MIN=2; [ "$run" -eq 2 ] && LAST_MIN=3

  echo "$(date -u +%H:%M:%S)  === run ${run} of 2 ==="
  row "run${run}_start" "" "" "run $run begins"

  # ---- phase 0: quiet baseline ------------------------------------------
  echo "$(date -u +%H:%M:%S)  run $run  phase 0: quiet baseline  (${PHASE_MIN} min)"
  row "run${run}_ph0_start" "" "" "quiet baseline — noise floor"
  END_S=$(( $(date +%s) + MAIN_S ))
  while [ "$(date +%s)" -lt "$END_S" ]; do sudo -v; sleep 10; done
  row "run${run}_ph0_end" "" "" ""

  # ---- phase 1: TCP upload -----------------------------------------------
  echo "$(date -u +%H:%M:%S)  run $run  phase 1: TCP upload  (${PHASE_MIN} min)"
  row "run${run}_ph1_start" "" "" "TCP upload to $SERVER — expect out_bps spike"
  iperf_run "run${run}_ph1" upload "$MAIN_S"
  row "run${run}_ph1_end" "" "" ""

  # ---- phase 2: settle ---------------------------------------------------
  echo "$(date -u +%H:%M:%S)  run $run  phase 2: settle  (1 min)"
  row "run${run}_ph2_start" "" "" "settle — link returns to idle"
  END_S=$(( $(date +%s) + 60 ))
  while [ "$(date +%s)" -lt "$END_S" ]; do sudo -v; sleep 10; done
  row "run${run}_ph2_end" "" "" ""

  # ---- phase 3: TCP download ---------------------------------------------
  echo "$(date -u +%H:%M:%S)  run $run  phase 3: TCP download  (${PHASE_MIN} min)"
  row "run${run}_ph3_start" "" "" "TCP download from $SERVER — expect in_bps spike"
  iperf_run "run${run}_ph3" download "$MAIN_S"
  row "run${run}_ph3_end" "" "" ""

  # ---- phase 4: post-test baseline ---------------------------------------
  echo "$(date -u +%H:%M:%S)  run $run  phase 4: post-test baseline  (${LAST_MIN} min)"
  row "run${run}_ph4_start" "" "" "post-test baseline"
  END_S=$(( $(date +%s) + $(to_s "$LAST_MIN") ))
  while [ "$(date +%s)" -lt "$END_S" ]; do sudo -v; sleep 10; done
  row "run${run}_ph4_end" "" "" "run $run complete"
done

echo "Test 4 finished at $(now)"