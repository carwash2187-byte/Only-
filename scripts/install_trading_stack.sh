#!/usr/bin/env bash
# Install the optional heavy-duty open-source trading stack.
#
# The bots/ package itself only needs pandas/numpy/requests/yfinance (already
# in this repo's dependencies). This script adds the big GitHub projects:
#
#   freqtrade + FreqAI  (github.com/freqtrade/freqtrade)  - self-retraining crypto bot
#   FinRL               (github.com/AI4Finance-Foundation/FinRL) - deep RL research
#   robin_stocks        (github.com/jmfernandes/robin_stocks)    - Robinhood (unofficial!)
#   ccxt                (github.com/ccxt/ccxt)             - 100+ crypto exchanges
#
# Usage: bash scripts/install_trading_stack.sh [--with-finrl]

set -euo pipefail

echo "==> Installing broker/exchange libraries (robin_stocks, ccxt)..."
pip install robin_stocks ccxt

echo "==> Installing freqtrade with FreqAI extras..."
pip install "freqtrade[freqai]" || {
    echo "freqtrade install failed (it needs TA-Lib). On Debian/Ubuntu try:"
    echo "  sudo apt-get install build-essential libta-lib0-dev  # or build ta-lib from source"
    echo "  pip install freqtrade[freqai]"
}

if [[ "${1:-}" == "--with-finrl" ]]; then
    echo "==> Installing FinRL (large: pulls torch + stable-baselines3)..."
    pip install finrl
fi

echo
echo "Done. Next steps:"
echo "  python -m bots demo                      # safe offline demo"
echo "  python -m bots train --symbol SPY        # train the RL agent"
echo "  python -m bots signals                   # smart-money consensus"
echo "  python -m bots trade                     # paper-trade one cycle"
echo "  freqtrade trade --config bots/learning/freqai/config-freqai.json \\"
echo "      --strategy AdaptiveLearnerStrategy --freqaimodel LightGBMRegressor"
