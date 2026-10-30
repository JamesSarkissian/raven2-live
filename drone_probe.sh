#!/bin/bash
# Listen-only probe for the Sky Rider Raven 2 Wi-Fi link.
# Run it while the Mac is joined to the drone's SkyRider_... network.
# It sends no flight, camera or control commands. It only:
#   - records the Mac's network info on the drone network
#   - pings the drone 3 times
#   - checks which TCP ports are open (connect + immediate close, no data sent)
#   - captures whatever the drone sends on its own for 30 seconds
# Results go to a probe_<time> folder next to this script.
# It changes no settings; nothing to undo except deleting that folder.

set -u
IF=en0
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/probe_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
LOG="$OUT/report.txt"
log() { echo "$@" | tee -a "$LOG"; }

log "== Raven 2 listen-only probe $(date) =="

SSID="$(sudo ipconfig getsummary "$IF" 2>/dev/null | awk -F' : ' '/ SSID :/{print $2; exit}')"
MYIP="$(ipconfig getifaddr "$IF" 2>/dev/null)"
# Toy drones often hand out an address without a router, so fall back to
# the DHCP server (the drone itself), then to .1 on the Mac's subnet.
GW="$(ipconfig getoption "$IF" router 2>/dev/null)"
[ -n "$GW" ] || GW="$(ipconfig getoption "$IF" server_identifier 2>/dev/null)"
[ -n "$GW" ] || { [ -n "${MYIP:-}" ] && GW="${MYIP%.*}.1"; }
log "Wi-Fi network: ${SSID:-unknown}"
log "Mac address:   ${MYIP:-none}"
log "Drone address: ${GW:-none}"

{
  echo "--- ifconfig $IF"; ifconfig "$IF"
  echo "--- DHCP packet"; ipconfig getpacket "$IF"
  echo "--- ARP table"; arp -an
} >> "$OUT/network.txt" 2>&1

if [ -z "${GW:-}" ]; then
  log "No gateway found. Is the Mac joined to the drone's Wi-Fi? Stopping."
  exit 1
fi
case "$GW" in
  192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|10.*) ;;
  *) log "Gateway $GW is not a private address. Stopping."; exit 1 ;;
esac

read -r -p "Is '${SSID:-unknown}' your drone's network? Props off, remote bound? [y/N] " ok
[ "$ok" = "y" ] || [ "$ok" = "Y" ] || { log "Cancelled by user."; exit 1; }

# Capture everything on the interface during the probe and 30 s of idle listening
sudo tcpdump -i "$IF" -n -s 0 -w "$OUT/capture.pcap" >/dev/null 2>&1 &
TCPDUMP_PID=$!
sleep 2

log "--- ping"
ping -c 3 -t 5 "$GW" 2>&1 | tee -a "$LOG"

# Ports commonly used by Wi-Fi toy drones for video, web and debug shells
PORTS="21 23 80 443 554 1234 5000 5555 6000 7060 7070 7080 8000 8080 8081 8554 8800 8888 9000 9001 40000 50000"
log "--- open TCP ports on $GW"
for p in $PORTS; do
  if nc -z -G 1 -w 1 "$GW" "$p" >/dev/null 2>&1; then log "OPEN $p"; fi
done

log "--- listening 30 s for traffic the drone sends on its own"
sleep 30

sudo kill "$TCPDUMP_PID" 2>/dev/null
wait "$TCPDUMP_PID" 2>/dev/null

log "--- traffic summary (top senders/ports)"
tcpdump -n -r "$OUT/capture.pcap" 2>/dev/null \
  | awk '{print $3, $5, $2}' | sort | uniq -c | sort -rn | head -40 | tee -a "$LOG"

log "Done. Results in: $OUT"
log "Reconnect to your normal Wi-Fi, then tell Claude the probe is finished."
