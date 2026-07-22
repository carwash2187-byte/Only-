#!/usr/bin/env bash
# Token-free resilience loop (session 47): keeps every bot alive WITHOUT any
# Claude/Routine involvement. The hourly keep-alive Routine keeps the remote
# container itself alive; this loop runs INSIDE the container and self-heals
# every 5 minutes in between -- it is plain bash + python, consumes no Claude
# usage, and keeps working even if the account's Claude usage runs out for
# as long as the container survives. All state is git-synced by keepalive.sh
# on every pass, so nothing is lost if the container is reclaimed.
#
# Launch (idempotent -- refuses to double-start):
#   nohup bash scripts/watchdog.sh >> /tmp/watchdog.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")/.."

LOCK=/tmp/bots_watchdog.pid
if [[ -f "$LOCK" ]] && kill -0 "$(cat "$LOCK")" 2>/dev/null; then
    echo "watchdog already running (pid $(cat "$LOCK")), exiting"
    exit 0
fi
echo $$ > "$LOCK"

echo "watchdog started $(date -u +%FT%TZ) (pid $$)"
while true; do
    out=$(bash scripts/keepalive.sh 2>&1)
    [[ -n "$out" ]] && echo "[$(date -u +%FT%TZ)] $out"
    sleep 300
done
