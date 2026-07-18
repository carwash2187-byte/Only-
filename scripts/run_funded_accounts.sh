#!/usr/bin/env bash
# Launch the two funded-account TradeLocker bots as fully isolated processes.
#
# Credentials live in gitignored env files (NEVER commit these):
#   bot_data/tradelocker_acct1.env
#   bot_data/tradelocker_acct2.env
# each containing:
#   TRADELOCKER_EMAIL=...
#   TRADELOCKER_PASSWORD=...
#   TRADELOCKER_SERVER=...
#   # TRADELOCKER_LIVE=1   <- only after demo has proven itself
#
# State (journal, Q-table, drawdown guards) is isolated per account in the
# git-committed funded_state_acct1/ and funded_state_acct2/ directories, so
# each account's limits and track record survive container restarts and can
# never bleed into the other's.
#
# Usage: bash scripts/run_funded_accounts.sh [acct1|acct2|both(default)]

set -euo pipefail
cd "$(dirname "$0")/.."

SYMBOLS="EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCHF,USDCAD,EURJPY,GBPJPY,AUDJPY,EURGBP,EURCHF,US30,NAS100,US500,GOLD,OIL"

launch() {
    local n="$1"
    local envfile="bot_data/tradelocker_acct${n}.env"
    local statedir="funded_state_acct${n}"
    if [[ ! -f "$envfile" ]]; then
        echo "acct${n}: $envfile not found -- create it with the TradeLocker credentials"
        return 1
    fi
    mkdir -p "$statedir"

    echo "acct${n}: running preflight (read-only)..."
    if ! (set -a; source "$envfile"; set +a; \
          BOT_DATA_DIR="$statedir" PYTHONPATH=. python scripts/preflight_funded.py); then
        echo "acct${n}: PREFLIGHT FAILED -- not starting this bot"
        return 1
    fi

    echo "acct${n}: starting autopilot (state in $statedir/)..."
    # --weekend-symbols none: Clarity Traders bans weekend trading/holding
    # without the "Trade on Weekends" add-on -- the bot sits weekends out.
    # (Their FAQ also requires the "EA's Allowed" add-on for bots AT ALL --
    # make sure it was purchased on this account before launching.)
    (set -a; source "$envfile"; set +a; \
     BOT_DATA_DIR="$statedir" nohup python -m bots autopilot \
        --broker tradelocker --funded --timeframe 1m --interval 1 \
        --market forex --symbols "$SYMBOLS" --weekend-symbols none \
        > "/tmp/autopilot_acct${n}.log" 2>&1 &)
    sleep 2
    if pgrep -f "autopilot.*tradelocker" > /dev/null; then
        echo "acct${n}: running (log: /tmp/autopilot_acct${n}.log)"
    else
        echo "acct${n}: FAILED to stay up -- check /tmp/autopilot_acct${n}.log"
        return 1
    fi
}

target="${1:-both}"
case "$target" in
    acct1) launch 1 ;;
    acct2) launch 2 ;;
    both)  launch 1; launch 2 ;;
    *) echo "usage: $0 [acct1|acct2|both]"; exit 1 ;;
esac
