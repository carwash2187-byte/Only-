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
    # Heartbeat (session 48): a timestamp any external monitor -- a free
    # UptimeRobot-style check, a cron job, or just a human -- can read to
    # confirm the loop is actually alive, not just assumed to be.
    date -u +%FT%TZ > /tmp/only_bots_heartbeat 2>/dev/null || true

    # Self-improvement law (session 47): once a day, retrain the live
    # Q-table on recent rough market windows -- plain python, no Claude
    # usage -- then bounce the paper bot so it trades on what it just
    # learned. The journal-driven parts (probation, cooldowns, mistakes
    # log, MFE/MAE) update themselves on every close already; this covers
    # the model itself.
    STAMP=/tmp/last_selftrain
    now=$(date +%s)
    last=$([[ -f "$STAMP" ]] && stat -c %Y "$STAMP" || echo 0)
    if (( now - last >= 86400 )); then
        touch "$STAMP"
        echo "[$(date -u +%FT%TZ)] nightly self-train starting"
        if BOT_DATA_DIR=paper_state PYTHONPATH=. timeout 3600 python scripts/stress_test.py --practice \
                >> /tmp/selftrain.log 2>&1; then
            pkill -f "autopilot --broker paper" || true
            sleep 2
            bash scripts/keepalive.sh
            echo "[$(date -u +%FT%TZ)] self-train done, paper bot restarted on the updated Q-table"
        else
            echo "[$(date -u +%FT%TZ)] PROBLEM: nightly self-train failed (see /tmp/selftrain.log)"
        fi
    fi
    sleep 300
done
