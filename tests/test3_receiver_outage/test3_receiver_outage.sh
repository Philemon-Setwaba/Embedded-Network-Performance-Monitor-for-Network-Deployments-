#!/bin/bash
# tests/test3_receiver_outage/test3_receiver_outage.sh
# Test 3: receiver outage - store-and-forward under a long outage (no NetEm).
# Phase 1 (OUTAGE_H hours, default 10): receiver OFF. netmon measures as normal,
#   vmagent cannot deliver and keeps everything in its buffer. Buffer logged every minute.
# Phase 2: you start the receiver on the laptop. The script waits for it (max WAIT_MAX_H),
#   then logs the catch-up every 5 s until the buffer is empty.
# Phase 3: 10 more minutes of normal running, then netmon stopped, buffer drained, vmagent stopped.
# victoria and grafana keep running so the results can be viewed.
#
# Run on the Pi itself, in a terminal you leave open:
#   bash tests/test3_receiver_outage/test3_receiver_outage.sh
# Trial run with a 5-minute outage:  OUTAGE_H=0.0833 bash tests/test3_receiver_outage/test3_receiver_outage.sh

set -u
OUTAGE_H=${OUTAGE_H:-10}
WAIT_MAX_H=${WAIT_MAX_H:-4}          # how long to wait for the receiver after the outage
AFTER_MIN=10                         # normal running after the buffer is empty
DRAIN_MAX_S=900
RX=http://netmon-receiver:8428/health
LOGDIR=$HOME/tests
mkdir -p "$LOGDIR"
LOG=$LOGDIR/test3_outage_$(date -u +%Y%m%dT%H%MZ).csv
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
metric() {  # sum of a vmagent metric
  curl -s -m 5 http://127.0.0.1:8429/metrics | awk -v m="$1" '$1 ~ "^"m {s+=$NF} END {printf "%.0f\n", s+0}'
}
rx_up() { [ "$(curl -s -m 5 $RX)" = "OK" ]; }
row() {     # phase,time,pending_bytes,disk_bytes,bytes_sent,rate_limit_reached,dropped,note
  echo "$1,$(now),$(metric vmagent_remotewrite_pending_data_bytes),$(sudo du -sb /var/lib/vmagent | cut -f1),$(metric vmagent_remotewrite_bytes_sent_total),$(metric vmagent_remotewrite_rate_limit_reached_total),$(( $(metric vmagent_remotewrite_packets_dropped_total) + $(metric vmagent_remotewrite_samples_dropped_total) )),$2" >> "$LOG"
}

cleanup() {
  trap - EXIT INT TERM
  sudo systemctl stop netmon
  row cleanup "systemctl stop netmon"
  t=0
  while [ "$(metric vmagent_remotewrite_pending_data_bytes)" != "0" ] && [ $t -lt $DRAIN_MAX_S ] && rx_up; do
    sleep 10; t=$((t + 10))
  done
  row drain "waited_s=$t"
  sudo systemctl stop vmagent
  echo "cleanup,$(now),,,,,,systemctl stop vmagent" >> "$LOG"
  echo "Done. Log: $LOG"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

echo "phase,time_utc,pending_bytes,disk_bytes,bytes_sent_total,rate_limit_reached_total,dropped,note" > "$LOG"
sudo -v || exit 1

# ---- checks --------------------------------------------------------------
rx_up && { echo "Receiver is ON - stop it on the laptop first (this test needs it OFF)."; exit 1; }
for s in netmon vmagent; do
  systemctl is-active --quiet $s && { echo "$s already running - stop it first."; exit 1; }
done
sudo tc qdisc del dev wlan0 root 2>/dev/null      # make sure no NetEm is left
sudo systemctl start vmagent; sleep 5               # vmagent BEFORE netmon
echo "start,$(now),,,,,,systemctl start vmagent" >> "$LOG"
sudo systemctl start netmon
row start "systemctl start netmon - outage begins (receiver off)"
END_S=$(( $(date +%s) + $(awk -v h=$OUTAGE_H 'BEGIN {printf "%d", h*3600}') ))
echo "Outage running for $OUTAGE_H h, until $(date -u -d @$END_S +%H:%M:%SZ). Log: $LOG"

# ---- phase 1: outage -----------------------------------------------------
while [ $(date +%s) -lt $END_S ]; do
  sudo -v
  sleep 60
  row outage ""
  rx_up && { row outage "receiver came up early - outage ended"; break; }
done
row outage_end "outage time over"

# ---- phase 2: wait for receiver, then catch-up ---------------------------
echo "$(date -u +%H:%M:%S)  OUTAGE DONE - start the receiver on the laptop now (laptop_services.py start)."
W_END=$(( $(date +%s) + $(awk -v h=$WAIT_MAX_H 'BEGIN {printf "%d", h*3600}') ))
until rx_up; do
  [ $(date +%s) -ge $W_END ] && { row wait "receiver did not come back within $WAIT_MAX_H h"; exit 1; }
  sudo -v; sleep 5
done
row receiver_back "receiver reachable"
echo "$(date -u +%H:%M:%S)  receiver back - logging catch-up"
C_END=$(( $(date +%s) + 7200 ))                     # catch-up safety limit: 2 h
while [ "$(metric vmagent_remotewrite_pending_data_bytes)" != "0" ]; do
  [ $(date +%s) -ge $C_END ] && { row catchup "buffer not empty after 2 h"; exit 1; }
  sudo -v; sleep 5
  row catchup ""
done
row catchup_done "buffer empty"
echo "$(date -u +%H:%M:%S)  buffer empty - $AFTER_MIN min normal running"

# ---- phase 3: normal running ---------------------------------------------
for i in $(seq $AFTER_MIN); do sudo -v; sleep 60; row after ""; done
echo "Test 3 finished at $(now)"
