#!/bin/bash
# tests/test1_packet_loss/test1_loss.sh
# Test 1: packet loss with NetEm on wlan0.
# Steps: 0 % (baseline), 10, 30, 50, 70, 90, 100 %, 0 % (recovery).
# Every step is logged with its UTC start and end time.
# At the end (also after Ctrl+C): NetEm removed, netmon stopped, vmagent
# allowed to send its buffer to the receiver, then vmagent stopped.
# victoria and grafana keep running so the results can be viewed.
#
# Run on the Pi itself (not over SSH/Tailscale - 100 % loss cuts it):
#   bash tests/test1_packet_loss/test1_loss.sh
# Trial run with 1-minute steps:  STEP_MIN=1 bash tests/test1_packet_loss/test1_loss.sh

set -u
IF=wlan0
STEP_MIN=${STEP_MIN:-10}
LEVELS="0 10 30 50 70 90 100 0"
DRAIN_MAX_S=900                      # wait at most 15 min for the buffer
LOGDIR=$HOME/tests
mkdir -p "$LOGDIR"
LOG=$LOGDIR/test1_loss_$(date -u +%Y%m%dT%H%MZ).csv
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
metric() {  # sum of a vmagent metric, e.g. metric vmagent_remotewrite_pending_data_bytes
  curl -s -m 5 http://127.0.0.1:8429/metrics | awk -v m="$1" '$1 ~ "^"m {s+=$NF} END {print s+0}'
}

cleanup() {
  trap - EXIT INT TERM
  sudo tc qdisc del dev $IF root 2>/dev/null
  echo "cleanup,,$(now),,netem removed" >> "$LOG"
  sudo systemctl stop netmon
  echo "cleanup,,$(now),,systemctl stop netmon" >> "$LOG"
  echo "netmon stopped. Waiting for vmagent to send its buffer..."
  t=0
  while [ "$(metric vmagent_remotewrite_pending_data_bytes)" != "0" ] && [ $t -lt $DRAIN_MAX_S ]; do
    sleep 10; t=$((t + 10))
  done
  P=$(metric vmagent_remotewrite_pending_data_bytes)
  D=$(( $(metric vmagent_remotewrite_packets_dropped_total) + $(metric vmagent_remotewrite_samples_dropped_total) ))
  echo "drain,,$(now),,pending_bytes=$P dropped=$D waited_s=$t" >> "$LOG"
  sudo systemctl stop vmagent
  echo "cleanup,,$(now),,systemctl stop vmagent" >> "$LOG"
  echo "Done. pending=$P bytes, dropped=$D. Log: $LOG"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

echo "step,loss_pct,start_utc,end_utc,command" > "$LOG"
sudo -v || exit 1                                  # ask for the password once

# ---- checks --------------------------------------------------------------
[ "$(curl -s -m 5 http://netmon-receiver:8428/health)" = "OK" ] \
  || { echo "Receiver not reachable - start it on the laptop."; exit 1; }
sudo modprobe sch_netem || { echo "NetEm not available."; exit 1; }
if ! systemctl is-active --quiet vmagent; then     # vmagent BEFORE netmon
  sudo systemctl start vmagent; sleep 5
  echo "start,,$(now),,systemctl start vmagent" >> "$LOG"
fi
if ! systemctl is-active --quiet netmon; then
  sudo systemctl start netmon
  echo "start,,$(now),,systemctl start netmon" >> "$LOG"
fi
echo "Checks OK. ${STEP_MIN} min per step. Log: $LOG"

# ---- steps ---------------------------------------------------------------
n=0
for L in $LEVELS; do
  sudo -v
  if [ "$L" -eq 0 ]; then
    CMD="tc qdisc del dev $IF root"
    sudo tc qdisc del dev $IF root 2>/dev/null
  else
    CMD="tc qdisc replace dev $IF root netem loss ${L}%"
    sudo $CMD
  fi
  S=$(now)
  echo "$(date -u +%H:%M:%S)  step $n: loss ${L} %  (for ${STEP_MIN} min)"
  sleep $((STEP_MIN * 60))
  echo "$n,$L,$S,$(now),$CMD" >> "$LOG"
  n=$((n + 1))
done
echo "Test 1 finished at $(now)"
