#!/bin/bash
# tests/test5_backhaul_outage/test5_backhaul_outage.sh
# (Run as "Test 6" during testing; it is Test 5 in the report. Its export folder is ~/T6.)
# Test 6: backhaul outage - the LTE link itself goes down (no NetEm).
# Unlike Test 3 (receiver off, internet still up), here mobile data is switched OFF
# in the router, so the Pi loses the internet AND the receiver, but the router
# stays reachable over the LAN. Two outages, you switch mobile data by hand:
#   Ph0 baseline   BASE_MIN (10)  everything normal
#   Ph1 outage 1   OUT1_MIN (10)  mobile data OFF
#   Ph2 recovery 1 REC1_MIN (25)  mobile data ON, buffer drains
#   Ph3 outage 2   OUT2_MIN (45)  mobile data OFF
#   Ph4 recovery 2 REC2_MIN (30)  mobile data ON (last time), buffer drains
# The script tells you on screen when to act (GET READY 60 s before, then DO THIS NOW).
# Press Enter after each action - that logs the exact time you did it.
# Logged every 10 s: internet UP/DOWN, router, receiver, vmagent buffer.
# At the end: netmon stopped, buffer drained, vmagent stopped, everything exported
# from the Pi DB and the laptop DB into ~/T6, packed as ~/T6.tar.gz + sha256.
# victoria and grafana keep running. Nothing is deleted.
#
# Before starting: laptop on res Wi-Fi (NOT the router), laptop_services started,
# router page 192.168.1.1 open in the Pi browser on the mobile-data switch.
#
# Run on the Pi itself, in a terminal you leave open:
#   bash tests/test5_backhaul_outage/test5_backhaul_outage.sh
# Trial run (about 8 min):
#   BASE_MIN=1 OUT1_MIN=1.5 REC1_MIN=2 OUT2_MIN=1.5 REC2_MIN=2 bash tests/test5_backhaul_outage/test5_backhaul_outage.sh

set -u
BASE_MIN=${BASE_MIN:-10}
OUT1_MIN=${OUT1_MIN:-10}
REC1_MIN=${REC1_MIN:-25}
OUT2_MIN=${OUT2_MIN:-45}
REC2_MIN=${REC2_MIN:-30}
TICK=10                              # log interval (s)
CHECK_S=90                           # after an action, check the internet changed state
DRAIN_MAX_S=600
ROUTER=192.168.1.1
RXBASE=http://netmon-receiver:8428
RX=$RXBASE/health
OUTDIR=$HOME/T6
LOGDIR=$HOME/tests
mkdir -p "$LOGDIR"
LOG=$LOGDIR/test6_backhaul_outage_$(date -u +%Y%m%dT%H%MZ).csv
STARTED=0
PENDING=""                           # action waiting for your Enter
PREV_NET=""

now()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
sast() { TZ=Africa/Johannesburg date -d @"$1" +%H:%M; }
utc()  { date -u -d @"$1" +%H:%MZ; }
mins() { awk -v m="$1" 'BEGIN {printf "%d", m*60}'; }
metric() {  # sum of a vmagent metric, from the metrics text in $M
  echo "$M" | awk -v m="$1" '$1 ~ "^"m {s+=$NF} END {printf "%.0f\n", s+0}'
}
net_state() { ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 || ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && echo UP || echo DOWN; }
router_up() { ping -c1 -W1 $ROUTER >/dev/null 2>&1; }
rx_up()     { [ "$(curl -s -m 3 $RX)" = "OK" ]; }
row() {     # phase,time,internet,router,receiver,pending_bytes,disk_bytes,bytes_sent,dropped,note
  M=$(curl -s -m 3 http://127.0.0.1:8429/metrics)
  local r=DOWN x=DOWN
  router_up && r=UP
  rx_up && x=UP
  echo "$1,$(now),$2,$r,$x,$(metric vmagent_remotewrite_pending_data_bytes),$(sudo du -sb /var/lib/vmagent | cut -f1),$(metric vmagent_remotewrite_bytes_sent_total),$(( $(metric vmagent_remotewrite_packets_dropped_total) + $(metric vmagent_remotewrite_samples_dropped_total) )),$3" >> "$LOG"
  LAST_R=$r; LAST_X=$x; LAST_P=$(metric vmagent_remotewrite_pending_data_bytes)
}
banner() {  # big on-screen instruction with a beep
  while read -r -t 0.1; do :; done 2>/dev/null      # drop any old key presses
  printf '\a'
  echo
  echo "##################################################################"
  echo "  $(TZ=Africa/Johannesburg date +%H:%M:%S) SAST   >>> $1 <<<"
  [ -n "${2:-}" ] && echo "  $2"
  echo "##################################################################"
  echo
}
wait_until() {  # sleep to epoch $1; if an action is pending, Enter logs it
  local left
  while :; do
    left=$(( $1 - $(date +%s) ))
    [ $left -le 0 ] && return
    if [ -n "$PENDING" ]; then
      if read -r -t $left; then
        echo "user,$(now),,,,,,,,pressed Enter: $PENDING" >> "$LOG"
        echo "  logged: $PENDING done at $(TZ=Africa/Johannesburg date +%H:%M:%S) SAST"
        PENDING=""
      fi
    else
      sleep $left
    fi
  done
}

# ---- phase runner --------------------------------------------------------
# $1 phase  $2 end epoch  $3 expected internet (UP/DOWN, - = no check)  $4 next action ("" = none)
run_phase() {
  local ph=$1 end=$2 expect=$3 next=$4
  local t0=$(date +%s) n=0 ready=0 checked=0 last_min=-1 net note el
  while [ $(date +%s) -lt $end ]; do
    net=$(net_state); note=""
    if [ "$net" != "$PREV_NET" ]; then note="internet_$net"; PREV_NET=$net; fi
    row "$ph" "$net" "$note"
    el=$(( $(date +%s) - t0 ))
    # check the action worked (repeat every minute while it has not)
    if [ "$expect" != "-" ] && [ $el -ge $CHECK_S ] && [ $checked -eq 0 ]; then
      if [ "$net" = "$expect" ]; then
        echo "  internet $expect confirmed"; checked=1
      elif [ $(( el / 60 )) -ne $last_min ]; then
        printf '\a'; echo "  WARNING: internet still $net - check mobile data is $([ $expect = DOWN ] && echo OFF || echo ON)"
      fi
    fi
    # status line every minute
    if [ $(( el / 60 )) -ne $last_min ]; then
      last_min=$(( el / 60 )); sudo -v
      printf '%s SAST | %-11s | %2d min left | internet %-4s | router %-4s | receiver %-4s | buffer %s B\n' \
        "$(TZ=Africa/Johannesburg date +%H:%M)" "$ph" $(( (end - $(date +%s) + 59) / 60 )) "$net" "$LAST_R" "$LAST_X" "$LAST_P"
    fi
    # warning 60 s before the next action
    if [ -n "$next" ] && [ $ready -eq 0 ] && [ $(( end - $(date +%s) )) -le 60 ]; then
      ready=1; printf '\a'
      echo ">>> GET READY: at $(sast $end) SAST you will $next"
    fi
    n=$((n + 1))
    wait_until $(( t0 + n * TICK < end ? t0 + n * TICK : end ))
  done
}

# ---- finish: stop, drain, export, pack -----------------------------------
finish() {
  trap - EXIT INT TERM
  [ $STARTED -eq 1 ] || exit 1
  sudo systemctl stop netmon
  row cleanup "$(net_state)" "systemctl stop netmon"
  if [ "$(net_state)" = DOWN ]; then
    banner "DO THIS NOW: turn mobile data ON in the router" "(internet is down - the buffer cannot drain)"
    t=0; until [ "$(net_state)" = UP ] || [ $t -ge 600 ]; do sudo -v; sleep 10; t=$((t + 10)); done
  fi
  echo "Draining buffer to the laptop (max $((DRAIN_MAX_S / 60)) min)..."
  t=0
  while [ "$(M=$(curl -s -m 3 http://127.0.0.1:8429/metrics); metric vmagent_remotewrite_pending_data_bytes)" != "0" ] && [ $t -lt $DRAIN_MAX_S ]; do
    sudo -v; sleep 10; t=$((t + 10))
    [ $((t % 30)) -eq 0 ] && { row drain "$(net_state)" ""; echo "  buffer $LAST_P B  (${t}s)"; }
  done
  row drain "$(net_state)" "waited_s=$t"
  sudo systemctl stop vmagent
  echo "cleanup,$(now),,,,,,,,systemctl stop vmagent" >> "$LOG"

  echo "Exporting Pi DB and laptop DB..."
  S=$(( T0 - 300 )); E=$(( $(date +%s) + 300 ))
  cd "$OUTDIR" || exit 1
  ex() {  # $1 file  $2 match  $3 db url
    curl -s --data-urlencode "match[]=$2" -d start=$S -d end=$E "$3/api/v1/export" | gzip > "$1"
  }
  ex export_pi5_127.0.0.1.jsonl.gz       '{device="pi5"}'     http://localhost:8428
  ex export_pi5_netmon-receiver.jsonl.gz '{device="pi5"}'     $RXBASE
  ex export_vmagent_pi.jsonl.gz          '{job="vmagent"}'    http://localhost:8428
  ex export_all_pi.jsonl.gz              '{__name__=~".+"}'   http://localhost:8428
  ex export_all_receiver.jsonl.gz        '{__name__=~".+"}'   $RXBASE
  TZ=UTC journalctl -u netmon -u vmagent --since "$(date -u -d @$S '+%Y-%m-%d %H:%M:%S')" \
    --until "$(date -u -d @$E '+%Y-%m-%d %H:%M:%S')" | gzip > journal.txt.gz
  cp "$LOG" .
  echo
  bad=0
  for f in *.gz; do
    l=$(zcat "$f" | wc -l)
    printf '  %-40s %8s lines\n' "$f" "$l"
    [ "$l" -eq 0 ] && { echo "  !!! $f IS EMPTY"; bad=1; }
  done
  [ $bad -eq 1 ] && { printf '\a'; echo "  !!! at least one export is empty - tell me before cleaning anything"; }
  cd "$HOME" && tar czf T6.tar.gz T6 && sha256sum T6.tar.gz > T6.tar.gz.sha256
  printf '\a'
  echo
  echo "=============================================="
  echo "  SAVE ON THE LAPTOP NOW"
  echo "  T6.tar.gz  sha256: $(cut -d' ' -f1 T6.tar.gz.sha256)"
  echo "=============================================="
  echo "Done. Log: $LOG"
}
trap finish EXIT
trap 'exit 1' INT TERM

echo "phase,time_utc,internet,router,receiver,pending_bytes,disk_bytes,bytes_sent_total,dropped,note" > "$LOG"
sudo -v || exit 1

# ---- checks --------------------------------------------------------------
[ -e "$OUTDIR" ] || [ -e "$HOME/T6.tar.gz" ] && { echo "~/T6 or ~/T6.tar.gz already exists - move it first."; exit 1; }
[ "$(curl -s -m 3 http://localhost:8428/health)" = "OK" ] || { echo "Victoria is not running on the Pi - start it first."; exit 1; }
rx_up || { echo "Receiver is OFF - start laptop_services on the laptop first (this test needs it ON)."; exit 1; }
[ "$(net_state)" = UP ] || { echo "No internet - turn mobile data ON first."; exit 1; }
router_up || { echo "Router $ROUTER not reachable."; exit 1; }
for s in netmon vmagent; do
  systemctl is-active --quiet $s && { echo "$s already running - stop it first."; exit 1; }
done
mkdir -p "$OUTDIR"
sudo tc qdisc del dev wlan0 root 2>/dev/null      # make sure no NetEm is left
sudo systemctl start vmagent; sleep 5               # vmagent BEFORE netmon
echo "start,$(now),,,,,,,,systemctl start vmagent" >> "$LOG"
sudo systemctl start netmon
STARTED=1
T0=$(date +%s)
A1=$(( T0 + $(mins $BASE_MIN) ))
A2=$(( A1 + $(mins $OUT1_MIN) ))
A3=$(( A2 + $(mins $REC1_MIN) ))
A4=$(( A3 + $(mins $OUT2_MIN) ))
END=$(( A4 + $(mins $REC2_MIN) ))
row start UP "systemctl start netmon - test begins"

clear
echo "================ TEST 6: LTE BACKHAUL OUTAGE ================"
echo "  SAST    UTC     what"
echo "  $(sast $T0)   $(utc $T0)  start - baseline, do nothing"
echo "  $(sast $A1)   $(utc $A1)  TURN MOBILE DATA OFF   (outage 1)"
echo "  $(sast $A2)   $(utc $A2)  TURN MOBILE DATA ON"
echo "  $(sast $A3)   $(utc $A3)  TURN MOBILE DATA OFF   (outage 2)"
echo "  $(sast $A4)   $(utc $A4)  TURN MOBILE DATA ON    (last time)"
echo "  $(sast $END)   $(utc $END)  test ends - then drain + export (~10 min)"
echo "  Press Enter right after each action."
echo "  Log: $LOG"
echo "============================================================="
echo

# ---- phases --------------------------------------------------------------
run_phase ph0_baseline  $A1  -    "TURN MOBILE DATA OFF (outage 1)"

banner "DO THIS NOW: turn mobile data OFF in the router (outage 1)" "then press Enter"
PENDING="mobile data OFF (outage 1)"; row action "$(net_state)" "instructed: mobile data OFF (outage 1)"
run_phase ph1_outage1   $A2  DOWN "TURN MOBILE DATA ON"

banner "DO THIS NOW: turn mobile data ON in the router" "then press Enter"
PENDING="mobile data ON (after outage 1)"; row action "$(net_state)" "instructed: mobile data ON (after outage 1)"
run_phase ph2_recovery1 $A3  UP   "TURN MOBILE DATA OFF (outage 2)"

banner "DO THIS NOW: turn mobile data OFF in the router (outage 2)" "then press Enter"
PENDING="mobile data OFF (outage 2)"; row action "$(net_state)" "instructed: mobile data OFF (outage 2)"
run_phase ph3_outage2   $A4  DOWN "TURN MOBILE DATA ON (last time)"

banner "DO THIS NOW: turn mobile data ON in the router (LAST TIME)" "then press Enter"
PENDING="mobile data ON (after outage 2)"; row action "$(net_state)" "instructed: mobile data ON (after outage 2)"
run_phase ph4_recovery2 $END UP   ""

row end "$(net_state)" "test time over"
echo "Test 6 finished at $(now) - stopping, draining and exporting..."