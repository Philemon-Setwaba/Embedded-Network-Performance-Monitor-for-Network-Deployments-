#!/bin/bash
# tests/emulation/test2_delay.sh
# Test 2: delay and delay variation with NetEm on wlan0.
# Steps: 0 (baseline), 100 ms, 100+-10, 100+-25, 100+-50, 300, 1000, 2500 ms, 0 (recovery).
# Every step is logged with its UTC start and end time.
# At the end (also after Ctrl+C): NetEm removed, netmon stopped, vmagent
# allowed to send its buffer to the receiver, then vmagent stopped.
# victoria and grafana keep running so the results can be viewed.
#
# Run on the Pi itself (not over SSH/Tailscale - 2500 ms delay makes it unusable):
#   bash tests/emulation/test2_delay.sh
# Trial run with 1-minute steps:  STEP_MIN=1 bash tests/emulation/test2_delay.sh

set -u
IF=wlan0
STEP_MIN=${STEP_MIN:-10}
STEPS="0:0 100:0 100:10 100:25 100:50 300:0 1000:0 2500:0 0:0"   # delay_ms:jitter_ms
DRAIN_MAX_S=900                      # wait at most 15 min for the buffer
LOGDIR=$HOME/tests
mkdir -p "$LOGDIR"
LOG=$LOGDIR/test2_delay_$(date -u +%Y%m%dT%H%MZ).csv
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
metric() {  # sum of a vmagent metric, e.g. metric vmagent_remotewrite_pending_data_bytes
  curl -s -m 5 http://127.0.0.1:8429/metrics | awk -v m="$1" '$1 ~ "^"m {s+=$NF} END {print s+0}'
}

cleanup() {
  trap - EXIT INT TERM
  sudo tc qdisc del dev $IF root 2>/dev/null
  echo "cleanup,,,$(now),,netem removed" >> "$LOG"
  sudo systemctl stop netmon
  echo "cleanup,,,$(now),,systemctl stop netmon" >> "$LOG"
  echo "netmon stopped. Waiting for vmagent to send its buffer..."
  t=0
  while [ "$(metric vmagent_remotewrite_pending_data_bytes)" != "0" ] && [ $t -lt $DRAIN_MAX_S ]; do
    sleep 10; t=$((t + 10))
  done
  P=$(metric vmagent_remotewrite_pending_data_bytes)
  D=$(( $(metric vmagent_remotewrite_packets_dropped_total) + $(metric vmagent_remotewrite_samples_dropped_total) ))
  echo "drain,,,$(now),,pending_bytes=$P dropped=$D waited_s=$t" >> "$LOG"
  sudo systemctl stop vmagent
  echo "cleanup,,,$(now),,systemctl stop vmagent" >> "$LOG"
  echo "Done. pending=$P bytes, dropped=$D. Log: $LOG"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

echo "step,delay_ms,jitter_ms,start_utc,end_utc,command" > "$LOG"
sudo -v || exit 1                                  # ask for the password once

# ---- checks --------------------------------------------------------------
[ "$(curl -s -m 5 http://netmon-receiver:8428/health)" = "OK" ] \
  || { echo "Receiver not reachable - start it on the laptop."; exit 1; }
sudo modprobe sch_netem || { echo "NetEm not available."; exit 1; }
if ! systemctl is-active --quiet vmagent; then     # vmagent BEFORE netmon
  sudo systemctl start vmagent; sleep 5
  echo "start,,,$(now),,systemctl start vmagent" >> "$LOG"
fi
if ! systemctl is-active --quiet netmon; then
  sudo systemctl start netmon
  echo "start,,,$(now),,systemctl start netmon" >> "$LOG"
fi
echo "Checks OK. ${STEP_MIN} min per step. Log: $LOG"

# ---- steps ---------------------------------------------------------------
n=0
for ST in $STEPS; do
  D=${ST%%:*}; J=${ST##*:}
  sudo -v
  if [ "$D" -eq 0 ]; then
    CMD="tc qdisc del dev $IF root"
    sudo tc qdisc del dev $IF root 2>/dev/null
  elif [ "$J" -eq 0 ]; then
    CMD="tc qdisc replace dev $IF root netem delay ${D}ms"
    sudo $CMD
  else
    CMD="tc qdisc replace dev $IF root netem delay ${D}ms ${J}ms"
    sudo $CMD
  fi
  S=$(now)
  echo "check,,,$S,,$(tc qdisc show dev $IF | head -1)" >> "$LOG"   # what the kernel applied
  echo "$(date -u +%H:%M:%S)  step $n: delay ${D} ms +- ${J} ms  (for ${STEP_MIN} min)"
  sleep $((STEP_MIN * 60))
  echo "$n,$D,$J,$S,$(now),$CMD" >> "$LOG"
  n=$((n + 1))
done
echo "Test 2 finished at $(now)"
