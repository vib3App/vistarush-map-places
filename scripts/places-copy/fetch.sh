#!/usr/bin/env bash
# Downloads one Geofabrik extract: fetch.sh <url> <file>.
#
# A transfer that stops or crawls is dropped and picked up where it stopped.
# On 2026-11-01 the North America run sat in its download for two hours on a
# file that had come down in 20 minutes an hour before; curl on its own waits
# on a dead connection until the job's time runs out.
set -euo pipefail
url=$1
out=$2

# Where Geofabrik sends us (a mirror for a continent, a dated file for a
# country) and the file's size and date there, fixed for every attempt so
# each resumed piece comes from the same file. Asked up to five times: on
# 2026-11-02 the Czech Republic's question went unanswered for a minute
# (curl 28, 0 bytes) and the region was skipped for the week, while
# Slovakia's, asked a moment later, was answered at once.
locate() {
  local head tries=1
  until head=$(curl -sS -I -L --fail --max-time 60 -o /dev/null \
    -w '%{url_effective}\n%header{content-length}\n%header{last-modified}' "$url"); do
    [ "$tries" -lt 5 ] || { echo "no answer from $url after $tries tries"; exit 1; }
    tries=$((tries + 1))
    echo "no answer from $url: asking again in 30 s (try $tries of 5)"
    sleep 30
  done
  src=$(sed -n 1p <<<"$head")
  size=$(sed -n 2p <<<"$head")
  stamp=$(sed -n 3p <<<"$head")
  echo "$src: $size bytes, $stamp"
  [[ $size =~ ^[0-9]+$ ]] || { echo "no size for $src"; exit 1; }
}
have() { if [ -f "$out" ]; then wc -c <"$out" | tr -d ' '; else echo 0; fi; }

locate
rm -f "$out"
# How far it has come, every five minutes, in the step's log. The sleep gets
# none of the step's output, so nothing holds the log open after the end.
(while sleep 300 </dev/null >/dev/null 2>&1; do echo "$(date -u +%H:%M) $(have) bytes"; done) &
ticker=$!
trap 'kill $ticker 2>/dev/null || true' EXIT

stuck=0
restarts=0
while [ "$(have)" != "$size" ]; do
  before=$(have)
  # Under 2 MB/s for two minutes counts as stopped: at that pace the North
  # America file alone would take three hours.
  got=$(curl -sS --fail -C - --speed-limit 2000000 --speed-time 120 \
    -w '%header{last-modified}' -o "$out" "$src") || true
  if [ -n "$got" ] && [ "$got" != "$stamp" ]; then
    echo "the file changed on the server ($stamp, now $got): starting over"
    restarts=$((restarts + 1))
    [ "$restarts" -le 3 ] || { echo "it keeps changing"; exit 1; }
    locate
    rm -f "$out"
    continue
  fi
  [ "$(have)" = "$size" ] && break
  if [ "$(have)" = "$before" ]; then stuck=$((stuck + 1)); else stuck=0; fi
  if [ "$stuck" -ge 5 ]; then
    echo "five attempts in a row got nothing: stopping at $(have) of $size bytes"
    exit 1
  fi
  echo "stopped at $(have) of $size bytes: picking it up again"
  sleep 5
done

# Geofabrik publishes a checksum for a country's file but none for a
# continent's (404, 2026-11-01). Without one, the size above and the cut,
# which reads every block of the file, are what stop a damaged download
# before anything is uploaded.
if curl -sS -L --fail --max-time 60 -o "$out.md5" "$url.md5" 2>/dev/null; then
  echo "$(cut -d' ' -f1 "$out.md5")  $out" | md5sum -c -
else
  echo "no checksum published for this file"
fi
ls -la "$out"
