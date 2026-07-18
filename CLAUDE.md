# Project conventions (bots/ stack)

This fork adds a live paper-trading bot stack in `bots/` on top of
TauricResearch/TradingAgents. See `bots/README.md` for the module tour and
`docs/DAY-TRADING-NOTES.md` for the running research log (numbered
"Session N" entries — read the most recent few before assuming something
hasn't been tried).

## State and data directory

- `BOT_DATA_DIR=paper_state` is the convention for this project's live
  account: it points state (journal, Q-table, account balance, mistakes
  log) at `paper_state/`, which is **committed to git** on purpose so the
  paper-trading track record survives across machines/sessions/restarts.
  Always export it before running any `bots` command against the live
  account.
- `bot_data/` (the default when `BOT_DATA_DIR` is unset) is gitignored —
  never commit anything under it, and never commit real broker credentials
  (`bot_data/alpaca.env`, etc.) anywhere.
- **This applies to every broker, not just `paper`** — including
  TradeLocker or any future funded-account connector. The daily/max
  drawdown guards already isolate their state per broker name
  automatically (`day_state_<broker>.json`, `max_drawdown_state_<broker>.json`
  — see `test_guard_state_isolated_per_broker_and_respects_bot_data_dir`),
  but that isolation only survives a container restart if `BOT_DATA_DIR`
  points at the git-committed `paper_state/` directory. Connecting a new
  broker without exporting `BOT_DATA_DIR=paper_state` first would put its
  guard memory in the gitignored, restart-fragile `bot_data/` instead —
  always export it before the first run against any new broker, real or
  demo.

## Running the live desk

```
BOT_DATA_DIR=paper_state python -m bots autopilot --broker paper --funded \
  --timeframe 1m --interval 1 --market forex \
  --symbols EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCHF,USDCAD,EURJPY,GBPJPY,AUDJPY,EURGBP,EURCHF,US30,NAS100,US500,GOLD,OIL
```

`--funded` applies `funded_account_config()` in `bots/organization.py` —
the full funded-account rule set (3% daily loss limit, 5% max drawdown,
ATR stops, session-aware pairs, higher-timeframe confirm, anti-martingale
sizing, drawdown taper, time stops, breakout-retest filter, high-conviction
cap override, Asian-session trade budget). This is the desk's actual
default; don't loosen it without a documented, evidenced reason (see the
session log for precedent on how past risk changes were justified).

## Testing

```
python -m pytest tests/test_bots.py -q
```

Full suite takes ~2-4 minutes (many tests fetch nothing live, but some
build synthetic multi-day OHLC frames). Run before committing any change
under `bots/`.

## Dashboard

```
BOT_DATA_DIR=paper_state PYTHONPATH=. python scripts/build_dashboard.py /tmp/dashboard.html
```

Then republish via the Artifact tool with `url:
"https://claude.ai/code/artifact/6c7d9571-42e1-4f11-95d7-9e1c96fda50f"` to
keep the same link alive instead of minting a new one.

## Working pattern for changes to the trading logic

1. Research first (WebSearch) — implement only findings with real
   evidence behind them, not folklore. Cite sources in the doc entry.
2. Implement + add tests in `tests/test_bots.py`.
3. Run the full suite.
4. Add a new "## Session N" entry to `docs/DAY-TRADING-NOTES.md` (find the
   current highest N first) describing what was researched, what was
   implemented, and why — including honest write-ups of ideas that were
   researched and *rejected* (e.g. scale-out exits in session 16).
5. Commit and push to `claude/ai-trading-bot-research-yolqhm` — never
   push trading-logic changes to any other branch.
6. Restart the live autopilot process on the new code (kill the old
   background process, relaunch with the same command) and, if a
   `CronCreate`/Routine keep-alive job references the old command string,
   update it too.

## Honesty norms (carried over from the whole project history)

- No feature ships with a "guaranteed profit" framing — there is no such
  thing, and claims like that (from MambaFX or anyone else) get the same
  "grade it from the journal, don't trust the marketing" treatment.
- Prefer widening the candidate watchlist over loosening a risk filter
  when the desk isn't trading enough — more shots on goal at the same bar,
  not a lower bar.
- Check real journal/dashboard numbers before making a risk-sizing change
  in either direction (looser or tighter). Don't guess.
