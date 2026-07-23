#!/usr/bin/env bash
# Exits 1 (and prints why) if the watchdog loop hasn't ticked in over
# STALE_MINUTES, or if the paper autopilot process isn't running. Wire this
# into a cron job (e.g. every 10 min) piped to `mail` for a free email
# alert, or into any uptime-monitor script -- it needs no network access
# itself, just local process/file checks.
#
#   */10 * * * * /opt/only-/scripts/deploy/healthcheck.sh || \
#       echo "trading desk is down" | mail -s "ALERT: bot down" you@example.com
set -uo pipefail

STALE_MINUTES=10
HEARTBEAT=/tmp/only_bots_heartbeat

if [[ ! -f "$HEARTBEAT" ]]; then
    echo "PROBLEM: no heartbeat file found -- watchdog may never have started"
    exit 1
fi

age_seconds=$(( $(date -u +%s) - $(date -u -d "$(cat "$HEARTBEAT")" +%s 2>/dev/null || echo 0) ))
if (( age_seconds > STALE_MINUTES * 60 )); then
    echo "PROBLEM: heartbeat is $((age_seconds / 60)) minutes old (watchdog loop may be stuck/dead)"
    exit 1
fi

if ! pgrep -f "autopilot --broker paper" >/dev/null; then
    echo "PROBLEM: heartbeat is fresh but the paper autopilot process is not running"
    exit 1
fi

echo "OK: watchdog heartbeat ${age_seconds}s old, autopilot running"
exit 0
