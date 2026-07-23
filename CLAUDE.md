# Project conventions (bots/ stack)

This fork adds a live paper-trading bot stack in `bots/` on top of
TauricResearch/TradingAgents. See `bots/README.md` for the module tour and
`docs/DAY-TRADING-NOTES.md` for the running research log (numbered
"Session N" entries — read the most recent few before assuming something
hasn't been tried).

## Always-on law (user directive, session 47; superseded runner in 48)

The bots must keep trading with **zero Claude/LLM involvement in the loop**
and survive Claude usage running out completely, not just a container
restart:

- The trading loop is token-free by construction: `--llm-committee` is off
  in every real launch command and must stay off for the live desk —
  signals come from the on-disk Q-table + indicator filters only. Never
  wire an LLM call into the live trading path.
- **Primary runner (session 48): GitHub Actions**, not a local process.
  `.github/workflows/trading-cycle.yml` (every 15 min) and
  `trading-selftrain.yml` (nightly) run `bots/` on GitHub's own free
  infrastructure — no server, no VPS bill, no Claude usage, genuinely
  survives Claude usage hitting zero. **Exactly ONE thing may run
  `desk.run_once()`/`python -m bots autopilot` against `paper_state/` at
  a time** — running the local autopilot process (or `scripts/watchdog.sh`,
  which restarts it) AT THE SAME TIME as the GitHub Actions workflows
  double-trades the account (two writers racing on the same state files).
  Do not relaunch the local autopilot/watchdog while the GitHub Actions
  workflows are enabled, and vice versa.
  - **Both workflow files must exist identically on the repo's DEFAULT
    branch** (`claude/smillin-repo-install-3jsep0` — an unrelated stale
    import branch, not the trading branch) for the `schedule:` trigger to
    fire at all — GitHub only reads cron schedules from the default
    branch's copy. Their `checkout`/`push` steps are pinned to
    `claude/ai-trading-bot-research-yolqhm` regardless, so the actual
    code/account touched is always correct either way. Keep both copies
    identical when editing either workflow.
  - Repo Settings → Actions → General → Workflow permissions needs "Read
    and write permissions" for the default `GITHUB_TOKEN` to push state
    back — check this first if a scheduled run's git push step fails.
- `scripts/watchdog.sh` + the systemd/VPS path
  (`scripts/deploy/setup_vps.sh`, `docs/DEPLOY-24-7.md`) remain documented
  as an alternative for anyone who wants a tighter 1-minute cadence via a
  persistent process instead of GitHub's 15-minute cron — but pick ONE
  runner, never two at once, for the reason above.
- The hourly keep-alive Routine's job (keeping the Claude Code container
  itself alive) is now optional, not load-bearing — GitHub Actions doesn't
  need this session's container to exist at all.

## Self-improvement + market-watch laws (user directive, session 47)

All autonomous, all token-free — "its own person" is the design goal:

- **Self-improvement law:** the desk must keep learning from its own
  trading with no Claude in the loop: journal-driven symbol probation and
  loss cooldowns update on every close; the mistakes log records every
  loser; every managed cycle records each open trade's max favorable /
  adverse excursion (`mfe:`/`mae:` tags) so exit rules are tuned from
  evidence later; and the watchdog runs a nightly Q-table retrain on the
  roughest + strongest real market windows (`scripts/stress_test.py
  --practice`) then bounces the paper bot onto the updated model.
- **Market-watch law:** the desk scans the full 19-symbol watchlist every
  minute around the clock (forex calendar), plus the weekend crypto
  fallback, plus the ForexFactory news calendar — which is used as a
  news-safety BLACKOUT, not a news-chasing trigger: session 44 researched
  news-chasing and rejected it on evidence (spread blowouts at release
  time are how funded accounts die). Don't re-add news-chasing without new
  evidence that clears that bar.
- **Evidence law:** any exit/target retune must cite the journal's MFE/MAE
  data (or equivalent measured evidence), not intuition — same standard as
  the risk-change rules above.

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
- **The two funded TradeLocker accounts** use their own committed state
  dirs: `BOT_DATA_DIR=funded_state_acct1` and
  `BOT_DATA_DIR=funded_state_acct2` (one per process — this is what keeps
  the two accounts' journals, Q-tables and drawdown limits fully separate;
  see `test_two_funded_accounts_of_the_same_broker_type_stay_fully_separate`).
  Launch both with `bash scripts/run_funded_accounts.sh`, which runs
  `scripts/preflight_funded.py` first and refuses to start a bot whose
  preflight fails. Credentials live only in the gitignored
  `bot_data/tradelocker_acct1.env` / `bot_data/tradelocker_acct2.env`.

## Running the live desk

```
BOT_DATA_DIR=paper_state python -m bots autopilot --broker paper --funded \
  --timeframe 1m --interval 1 --market forex \
  --symbols EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCHF,USDCAD,EURJPY,GBPJPY,AUDJPY,EURGBP,EURCHF,US30,NAS100,US500,US2000,GOLD,SILVER,OIL
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

## Funded-account rule presets are LAW, not suggestions (user directive, session 48)

Once a prop firm's real rules are confirmed (their own site/FAQ/ToS, not
a secondhand review), they get codified as a `bots/organization.py`
preset function and treated as binding for that account — never trade
that firm's account on hand-tuned settings that drift from its actual
rules. Presets so far:

- `clarity_one_step_challenge_config()` — Clarity Traders One-Step: 10%
  target / 4% daily / 6% max during the challenge, 4%/10% once funded,
  no weekend trading, EA's Allowed add-on required (session 46).
- `aquafunded_instant_config()` — AquaFunded Instant Funded: 3% daily
  loss / 6% max total drawdown, 1:50 broker leverage ceiling (the desk's
  own `max_leverage` stays at the conservative default, well under
  that), no challenge phase (no target to lock), payout on demand, news
  trading permitted (blackout kept as risk discipline, not a
  requirement). EA policy confirmed straight from AquaFunded's own help
  center: allowed for "your own personal trading strategy," not HFT/
  latency-arbitrage/mass-market commercial EAs — this desk qualifies
  (session 48).

Adding a new firm's account = add a new preset the same way, cite the
source (their own page, not a review aggregator) in the docstring, add a
test asserting the numbers, and flag in the docstring that live numbers
should be re-verified against the firm's actual current page before
connecting real money — firms change terms without notice.

**Two-key live-trading law (session 48):** real money on a funded account
requires TWO independent things: (1) the account's credential secrets
(demo-only on their own), and (2) a separate go-live secret
(`AQUAFUNDED_GO_LIVE` = exactly `LIVE-I-UNDERSTAND-THE-RISK`) that only
the account owner can add via GitHub repo settings. The workflow derives
`TRADELOCKER_LIVE` from that secret at run time — no committed file or
code path ever hardcodes live mode, and deleting the secret stands the
account down to demo on the next cycle. Claude must never add, request
the value of, or work around this secret; the owner adding it IS the
consent. Also learned the hard way this session: GitHub's scheduler
minimum is 5 minutes — a sub-`*/5` cron silently never fires.

## Honesty norms (carried over from the whole project history)

- No feature ships with a "guaranteed profit" framing — there is no such
  thing, and claims like that (from MambaFX or anyone else) get the same
  "grade it from the journal, don't trust the marketing" treatment.
- Prefer widening the candidate watchlist over loosening a risk filter
  when the desk isn't trading enough — more shots on goal at the same bar,
  not a lower bar.
- Check real journal/dashboard numbers before making a risk-sizing change
  in either direction (looser or tighter). Don't guess.
