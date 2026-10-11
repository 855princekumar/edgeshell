#!/usr/bin/env bash
# EdgeShell watchdog (runs as root under systemd).
# Restarts edgeshell only after MAX consecutive failed health checks, writes a
# reason marker first so the restart is logged in the DB as 'watchdog'.
# Never restarts a service an admin stopped on purpose (inactive, not failed).
set -uo pipefail
URL="${HEALTH_URL:-http://127.0.0.1:5000/healthz}"
MARKER="${MARKER:-/var/lib/edgeshell/restart-reason.json}"
INTERVAL="${WATCHDOG_INTERVAL:-30}"
MAX="${WATCHDOG_MAX_FAILS:-3}"
GRACE="${WATCHDOG_GRACE:-90}"

log() { echo "watchdog: $*"; }
sleep "$GRACE"
fails=0
while true; do
  if curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; then
    fails=0
  else
    fails=$((fails + 1))
    log "health check failed (${fails}/${MAX}) at ${URL}"
    if [ "$fails" -ge "$MAX" ]; then
      if systemctl is-active --quiet edgeshell || systemctl is-failed --quiet edgeshell; then
        printf '{"reason":"watchdog","detail":"Health check failed %s times in a row at %s"}\n' \
          "$fails" "$URL" > "$MARKER"
        chown edgeshell:edgeshell "$MARKER" 2>/dev/null || true
        log "restarting edgeshell"
        systemctl reset-failed edgeshell 2>/dev/null || true
        systemctl restart edgeshell
        fails=0
        sleep "$GRACE"
      else
        log "edgeshell is stopped on purpose; not restarting"
        fails=0
      fi
    fi
  fi
  sleep "$INTERVAL"
done
