#!/usr/bin/env bash
# tests/count.sh
# Compares how many samples the Pi (local VictoriaMetrics) and the remote
# receiver (laptop, via netmon-receiver) hold for ONE time window.
# Equal numbers = everything measured in that window was delivered.
#
# Usage (on the Pi, times in UTC):
#   bash tests/count.sh 2026-10-16T09:00:00Z 2026-10-16T11:00:00Z

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: bash tests/count.sh START_UTC END_UTC"
  exit 1
fi
START="$1"
END="$2"

SECONDS_LEN=$(( $(date -u -d "$END" +%s) - $(date -u -d "$START" +%s) ))
if (( SECONDS_LEN <= 0 )); then
  echo "Error: END must be after START"
  exit 1
fi

QUERY="sum(count_over_time({device=\"pi5\"}[${SECONDS_LEN}s]))"

count_on() {
  { curl -s --max-time 10 "http://$1:8428/api/v1/query" \
         --data-urlencode "query=$QUERY" \
         --data-urlencode "time=$END" || true; } \
  | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)["data"]["result"]
    print(r[0]["value"][1] if r else 0)
except Exception as e:
    print("error:", e)
'
}

PI=$(count_on localhost)
REMOTE=$(count_on netmon-receiver)

echo "Window:  $START  ->  $END  (${SECONDS_LEN} s)"
echo "Pi:      $PI"
echo "Remote:  $REMOTE"
if [[ ! "$PI" =~ ^[0-9]+$ || ! "$REMOTE" =~ ^[0-9]+$ ]]; then
  echo "Result:  CANNOT COMPARE (a database did not answer)"
elif [[ "$PI" == "$REMOTE" ]]; then
  echo "Result:  EQUAL - 100 % delivered"
else
  echo "Result:  DIFFERENT"
fi
