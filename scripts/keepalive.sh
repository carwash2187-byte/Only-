#!/usr/bin/env bash
# One self-healing pass for EVERY bot this repo runs -- called hourly by the
# keep-alive Routine. Covers the paper bot AND both funded TradeLocker bots
# (any account whose credentials file exists). Also git-syncs every state
# directory so journals/Q-tables/guard limits survive container loss.
#
# Output contract (the Routine reads this):
#   - no output            -> everything was already fine, stay silent
#   - RESTARTED/SYNCED ... -> self-healed, brief note is fine
#   - PROBLEM ...          -> needs the user's attention
set -uo pipefail
cd "$(dirname "$0")/.."

SYMBOLS="EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCHF,USDCAD,EURJPY,GBPJPY,AUDJPY,EURGBP,EURCHF,US30,NAS100,US500,GOLD,OIL"

sync_state() {
    local dir="$1"
    [[ -d "$dir" ]] || return 0
    if [[ -n "$(git status --porcelain "$dir" 2>/dev/null)" ]]; then
        git add "$dir" && git commit -m "Sync $dir" -q
        local ok=""
        for i in 1 2 3 4; do
            git push -u origin claude/ai-trading-bot-research-yolqhm -q && { ok=1; break; }
            sleep $((2**i))
        done
        if [[ -n "$ok" ]]; then echo "SYNCED: $dir"
        else echo "PROBLEM: could not push $dir state to GitHub"; fi
    fi
}

# --- paper bot -------------------------------------------------------------
if ! pgrep -f "autopilot --broker paper" >/dev/null; then
    BOT_DATA_DIR=paper_state nohup python -m bots autopilot --broker paper --funded \
        --timeframe 1m --interval 1 --market forex --symbols "$SYMBOLS" \
        >> /tmp/autopilot_paper.log 2>&1 &
    disown || true
    sleep 3
    if pgrep -f "autopilot --broker paper" >/dev/null; then
        echo "RESTARTED: paper bot was down"
    else
        echo "PROBLEM: paper bot failed to restart (see /tmp/autopilot_paper.log)"
    fi
fi
sync_state paper_state

# --- funded TradeLocker bots (only accounts with credentials on disk) ------
for n in 1 2; do
    envfile="bot_data/tradelocker_acct${n}.env"
    [[ -f "$envfile" ]] || continue
    pidfile="/tmp/funded_acct${n}.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        :  # alive
    else
        # run_funded_accounts.sh preflights first and refuses a bot whose
        # account/connection is broken -- an hourly retry loop, not a
        # blind relaunch with real money on the line.
        if bash scripts/run_funded_accounts.sh "acct${n}" >> "/tmp/keepalive_acct${n}.log" 2>&1; then
            echo "RESTARTED: funded acct${n} bot was down (preflight passed)"
        else
            echo "PROBLEM: funded acct${n} bot is down and preflight/restart FAILED -- check /tmp/keepalive_acct${n}.log"
        fi
    fi
    sync_state "funded_state_acct${n}"
done
