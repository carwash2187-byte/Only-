"""Command line for the bot stack.

    python -m bots demo                     # offline demo on synthetic data
    python -m bots train --symbol AAPL      # train the RL agent on real data
    python -m bots signals                  # show who the smart money is buying
    python -m bots trade                    # one desk cycle (paper broker)
    python -m bots trade --broker alpaca    # one desk cycle on Alpaca paper keys
    python -m bots journal                  # performance + lessons learned
"""

from __future__ import annotations

import argparse


def cmd_demo(_args) -> None:
    import numpy as np
    import pandas as pd

    from bots.brokers import PaperBroker
    from bots.journal import TradeJournal
    from bots.learning import QTraderAgent
    from bots.organization import DeskConfig, TradingDesk

    rng = np.random.default_rng(7)
    steps = 400
    prices = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, steps)))
    df = pd.DataFrame({"close": prices})

    print("1) Training RL agent on synthetic price history (learning by mistake)...")
    agent = QTraderAgent(model_path="bot_data/demo_qtable.json")
    stats = agent.train(df, episodes=25)
    print(
        f"   after training: {stats['trades']} trades, win rate {stats['win_rate']:.0%}, "
        f"return {stats['total_return_pct']:+.1f}%"
    )

    print("2) Running one trading-desk cycle on a paper account...")
    last = float(df['close'].iloc[-1])
    broker = PaperBroker(
        starting_cash=10_000,
        state_path="bot_data/demo_paper_account.json",
        price_overrides={"DEMO": last},
    )
    journal = TradeJournal(path="bot_data/demo_journal.json")
    from bots.risk import DrawdownGuard

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=agent,
        config=DeskConfig(news_blackout=False, min_copy_score=0),
        history_fn=lambda _symbol: df,
        guard=DrawdownGuard(state_path="bot_data/demo_day_state.json"),
        manual_signals_path="bot_data/demo_manual_signals.json",
    )
    report = desk.run_once(symbols=["DEMO"])
    print(report.describe())

    print("3) Journal so far:")
    print(journal.summary())
    print("\nDemo done - everything ran locally with fake money.")


def cmd_train(args) -> None:
    from bots.learning import train as train_mod

    df = train_mod.fetch_history(args.symbol, args.period, args.interval)
    from bots.learning import QTraderAgent

    agent = QTraderAgent()
    agent.load()
    stats = agent.train(df, episodes=args.episodes)
    agent.save()
    print(
        f"Trained on {args.symbol} ({args.interval} bars): {stats['trades']} trades, "
        f"win rate {stats['win_rate']:.0%}, return {stats['total_return_pct']:+.1f}% | "
        f"latest signal: {agent.signal(df)}"
    )


def cmd_signals(_args) -> None:
    from bots.copytrader import consensus_signals

    signals = consensus_signals()
    if not signals:
        print("No signals available (data feeds unreachable?).")
        return
    print("Smart-money consensus (13F whales + Congress purchases):")
    for signal in signals:
        print("  " + signal.describe())


def cmd_trade(args) -> None:
    from bots.brokers import get_broker
    from bots.organization import DeskConfig, TradingDesk

    # Realistic spread cost on the paper broker (session 27): frictionless
    # fills made every number here more optimistic than a real account.
    broker_kwargs = {"model_spread": True} if args.broker == "paper" else {}
    broker = get_broker(args.broker, **broker_kwargs)
    if not broker.is_paper and not args.live_i_understand_the_risk:
        raise SystemExit(
            f"Broker '{args.broker}' trades REAL money and has no paper mode. "
            "Re-run with --live-i-understand-the-risk to proceed."
        )
    config = DeskConfig(use_llm_committee=args.llm_committee)
    desk = TradingDesk(broker=broker, config=config)
    symbols = args.symbols.split(",") if args.symbols else None
    report = desk.run_once(symbols=symbols)
    print(report.describe())


def cmd_backtest(args) -> None:
    from bots.learning.backtest import run_backtest
    from bots.learning.train import fetch_history

    df = fetch_history(args.symbol, args.period, args.interval)
    r = run_backtest(df, episodes=args.episodes)
    verdict = "BEAT" if r["edge_vs_buy_hold_pct"] > 0 else "LOST TO"
    print(
        f"{args.symbol} ({args.interval}) out-of-sample: {r['trades']} trades, "
        f"win rate {r['win_rate']:.0%}, agent {r['agent_return_pct']:+.2f}% vs "
        f"buy-hold {r['buy_hold_return_pct']:+.2f}% -> {verdict} by "
        f"{abs(r['edge_vs_buy_hold_pct']):.2f}%"
    )


def cmd_practice(args) -> None:
    from bots.learning import QTraderAgent
    from bots.learning.scenarios import run_practice

    agent = None
    if args.save:
        # harden the LIVE Q-table in place, then persist it. Opt-in only:
        # synthetic training must never silently overwrite the learned policy.
        agent = QTraderAgent()
        agent.load()
    r = run_practice(
        n_scenarios=args.scenarios,
        episodes_per=args.episodes,
        agent=agent,
        seed=args.seed,
    )
    print(
        f"Practised across {r['scenarios']} synthetic sessions "
        f"({r['total_trades']} trades, overall win rate {r['overall_win_rate']:.0%}, "
        f"avg {r['avg_return_pct']:+.2f}%/session)."
    )
    print("Synthetic returns are NOT a track record -- this hardens the policy "
          "and shows where it bleeds. Grade real money from the journal.\n")
    print(f"{'regime':<18}{'sessions':>9}{'trades':>8}{'win%':>7}{'avg ret%':>10}")
    for name, s in sorted(
        r["by_regime"].items(), key=lambda kv: kv[1]["avg_return_pct"]
    ):
        print(
            f"{name:<18}{s['scenarios']:>9}{s['trades']:>8}"
            f"{s['win_rate'] * 100:>6.0f}%{s['avg_return_pct']:>+10.2f}"
        )
    if r["worst_regime"]:
        print(f"\nweakest tape: {r['worst_regime']}   strongest tape: {r['best_regime']}")
    if args.save and agent is not None:
        agent.save()
        print(f"\nlive Q-table hardened and saved ({agent.trained_episodes} total episodes).")


def cmd_challenge_odds(args) -> None:
    from bots.learning import QTraderAgent

    agent = QTraderAgent()
    agent.load()

    if args.real:
        from bots.learning.challenge_sim_real import run_real_data_monte_carlo

        r = run_real_data_monte_carlo(
            n_attempts=args.attempts,
            bars_per_attempt=args.bars,
            agent=agent,
            seed=args.seed,
            target_pct=args.target,
            daily_loss_pct=args.daily_loss,
            max_drawdown_pct=args.max_drawdown,
            risk_per_trade_pct=args.risk_per_trade,
        )
        print(f"symbols used: {', '.join(r['symbols_used'])}")
    else:
        from bots.learning.challenge_sim import run_challenge_monte_carlo

        r = run_challenge_monte_carlo(
            n_attempts=args.attempts,
            bars_per_attempt=args.bars,
            agent=agent,
            seed=args.seed,
            target_pct=args.target,
            daily_loss_pct=args.daily_loss,
            max_drawdown_pct=args.max_drawdown,
            risk_per_trade_pct=args.risk_per_trade,
        )
    print(
        f"{r['attempts']} simulated attempts (target {args.target:.0%}, "
        f"daily loss {args.daily_loss:.0%}, max drawdown {args.max_drawdown:.0%}, "
        f"risk/trade {args.risk_per_trade:.2%}):"
    )
    print(f"  PASS:      {r['pass_rate']:.1%}")
    print(f"  FAIL:      {r['fail_rate']:.1%}")
    print(f"  undecided: {r['undecided_rate']:.1%}")
    print(f"  avg final gain: {r['avg_final_gain_pct']:+.2f}%")
    if r.get("avg_bars_to_pass"):
        print(f"  avg bars to pass (winners only): {r['avg_bars_to_pass']:.0f}")
    label = (
        "block-bootstrapped REAL market history (independent check -- see "
        "docs/DAY-TRADING-NOTES.md session 46)"
        if args.real else
        "synthetic multi-regime tape (same generator family used to train -- "
        "run with --real for an independent check)"
    )
    print(f"\nSimulated on {label}, with the live-trained "
          "policy -- not a guarantee, and doesn't include every desk filter "
          "(ADX/zone/ORB/etc). See docs/DAY-TRADING-NOTES.md session 46.")


def cmd_journal(_args) -> None:
    from bots.journal import TradeJournal

    print(TradeJournal().summary() or "Journal is empty.")


def cmd_payout(_args) -> None:
    from bots.journal import TradeJournal

    r = TradeJournal().payout_readiness()
    print(f"realized profit: ${r['profit']:.2f}")
    print(f"days since first trade: {r['days_since_first_trade']}")
    print(f"trading days: {r['trading_days']}")
    print(f"best day share of profit: {r['best_day_share']:.0%}")
    if r["eligible"]:
        print("READY TO CASH OUT -- request the payout from the firm's dashboard")
    else:
        print("not payable yet:")
        for b in r["blockers"]:
            print(f"  - {b}")


def resolve_weekend_symbols(raw: str, market: str, funded: bool):
    """Weekend crypto fallback selection. "none" disables it entirely: some
    prop firms (Clarity Traders among them) ban weekend trading outright
    unless a specific add-on was purchased -- for those accounts the bot
    must sit the weekend out, not find something else to trade."""
    if raw.strip().lower() == "none":
        return None
    if raw:
        return raw.split(",")
    return ["BTC-USD", "ETH-USD", "SOL-USD"] if (market == "forex" and funded) else None


def cmd_autopilot(args) -> None:
    from bots.autopilot import run_autopilot
    from bots.brokers import get_broker
    from bots.organization import DeskConfig, TradingDesk

    # Law: funded accounts trade 1-minute candles, matching MambaFX's own
    # documented timeframe -- not just when --timeframe 1m happens to be
    # passed, but as the actual fallback if it's ever omitted.
    if args.funded:
        timeframe = args.timeframe or "1m"
    else:
        timeframe = args.timeframe or ("5m" if args.day_trading else "1d")
    if args.funded:
        from bots.organization import funded_account_config

        config = funded_account_config(use_llm_committee=args.llm_committee, timeframe=timeframe)
    elif args.day_trading:
        # Scalper-shaped defaults (see docs/DAY-TRADING-NOTES.md): tight stop,
        # 1:2 risk:reward, capped trade count, intraday candles.
        config = DeskConfig(
            use_llm_committee=args.llm_committee,
            day_trading=True,
            timeframe=timeframe,
            stop_loss_pct=0.015,
            take_profit_pct=0.03,
            max_trades_per_day=10,
            max_consecutive_losses=2,
            atr_stops=True,
        )
    else:
        config = DeskConfig(
            use_llm_committee=args.llm_committee,
            timeframe=timeframe,
        )
    # Realistic spread cost on the paper broker (session 27): the desk's
    # numbers had been computed on frictionless fills, which no real
    # account gets. Real brokers charge a real spread on every trade.
    # Leverage mirrors the config's exposure cap (session 47) so the paper
    # account rehearses with the same margin a funded account would have.
    broker_kwargs = (
        {"model_spread": True, "leverage": config.max_leverage}
        if args.broker == "paper" else {}
    )
    broker = get_broker(args.broker, **broker_kwargs)
    if not broker.is_paper and not args.live_i_understand_the_risk:
        raise SystemExit(
            f"Broker '{args.broker}' trades REAL money. "
            "Re-run with --live-i-understand-the-risk to proceed."
        )
    desk = TradingDesk(broker=broker, config=config)
    # Re-enabled by explicit user request (session 27) after session 19
    # found real 2026 evidence that weekend crypto liquidity has gotten
    # WORSE since spot ETFs concentrated institutional market-making into
    # weekday hours (+11% trading costs, -9% depth, real cascading-
    # liquidation events on thin weekend books). Rather than either
    # ignore that evidence or refuse continuous coverage, weekend crypto
    # trades now carry an explicit extra caution (DeskConfig.weekend_crypto_caution,
    # on by default) that halves position size specifically during this
    # window -- 24/7 coverage without pretending the weekend risk isn't real.
    weekend_symbols = resolve_weekend_symbols(
        args.weekend_symbols, args.market, args.funded
    )
    from bots.marketdata import resolve_symbol

    symbols = (
        [resolve_symbol(s) for s in args.symbols.split(",")] if args.symbols else None
    )
    stock_symbols = (
        [resolve_symbol(s) for s in args.stock_symbols.split(",")] if args.stock_symbols else (
            ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"] if (args.market == "forex" and args.funded) else None
        )
    )
    run_autopilot(
        broker_name=args.broker,
        interval_minutes=args.interval,
        symbols=symbols,
        stock_symbols=stock_symbols,
        desk=desk,
        market_override=args.market,
        weekend_symbols=weekend_symbols,
    )


def cmd_watch(args) -> None:
    from bots.copytrader import manual
    from bots.social import extract_calls

    found_any = False

    if args.youtube:
        from bots.social import youtube

        try:
            channel_id = youtube.resolve_channel_id(args.youtube)
            for video in youtube.recent_videos(channel_id, limit=args.limit):
                text = youtube.transcript_text(video.video_id) or video.title
                for call in extract_calls(text):
                    found_any = True
                    sig = manual.add_signal(
                        call.symbol, side=call.side, source=f"youtube:{args.youtube}",
                        note=f'"{video.title}": {call.snippet}',
                    )
                    print(f"[youtube] queued {sig.side.upper()} {sig.symbol} from '{video.title}'")
        except Exception as exc:
            print(f"[youtube] error: {exc}")

    if args.twitter:
        from bots.social import twitter

        try:
            for tweet in twitter.recent_tweets(args.twitter, limit=args.limit):
                for call in extract_calls(tweet.text):
                    found_any = True
                    sig = manual.add_signal(
                        call.symbol, side=call.side, source=f"twitter:{args.twitter}",
                        note=call.snippet,
                    )
                    print(f"[twitter] queued {sig.side.upper()} {sig.symbol}: {call.snippet}")
        except Exception as exc:
            print(f"[twitter] error: {exc}")

    if args.instagram:
        from bots.social import instagram

        try:
            fetcher = instagram.recent_stories if args.stories else instagram.recent_posts
            for post in fetcher(args.instagram):
                for call in extract_calls(post.caption):
                    found_any = True
                    sig = manual.add_signal(
                        call.symbol, side=call.side, source=f"instagram:{args.instagram}",
                        note=call.snippet,
                    )
                    print(f"[instagram] queued {sig.side.upper()} {sig.symbol}: {call.snippet}")
        except Exception as exc:
            print(f"[instagram] error: {exc}")

    if not (args.youtube or args.twitter or args.instagram):
        print("Nothing to watch -- pass --youtube/--twitter/--instagram (a channel/handle).")
    elif not found_any:
        print("Checked all sources, no trade calls detected this pass.")
    else:
        print("Run `python -m bots trade` to execute queued calls under the desk's risk rules.")


def cmd_mirror(args) -> None:
    from bots.copytrader import manual

    if args.list or not args.symbol:
        pending = manual.pending_signals()
        if not pending:
            print("No pending mirror calls. Record one with: "
                  "python -m bots mirror EURUSD --side buy --source mambafx")
            return
        for s in pending:
            print(f"  {s.side.upper():4s} {s.symbol:8s} from '{s.source}' ({s.added[:10]}) {s.note}")
        return
    if args.clear:
        manual.consume_signal(args.symbol)
        print(f"Cleared mirror call for {args.symbol.upper()}.")
        return
    signal = manual.add_signal(args.symbol, side=args.side, source=args.source, note=args.note)
    print(
        f"Recorded: {signal.side.upper()} {signal.symbol} from '{signal.source}'.\n"
        f"Next `python -m bots trade` will execute it under the desk's risk rules\n"
        f"(1% risk per trade, 5% daily circuit breaker), and the journal will track\n"
        f"'{signal.setup}' performance -- if this source keeps losing, it gets blocked."
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="bots", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="offline end-to-end demo with fake money")

    p_train = sub.add_parser("train", help="train the RL agent on real history")
    p_train.add_argument("--symbol", default="SPY")
    p_train.add_argument("--period", default="2y")
    p_train.add_argument("--interval", default="1d",
                         help="candle size, e.g. 1d (swing) or 5m/15m (day trading)")
    p_train.add_argument("--episodes", type=int, default=50)

    sub.add_parser("signals", help="show smart-money consensus buy signals")

    p_trade = sub.add_parser("trade", help="run one trading-desk cycle")
    p_trade.add_argument("--broker", default="paper",
                         choices=["paper", "alpaca", "robinhood", "crypto", "oanda", "tradelocker", "mt5"])
    p_trade.add_argument("--symbols", default="",
                         help="comma-separated watchlist (default: copy-trade signals)")
    p_trade.add_argument("--llm-committee", action="store_true",
                         help="also ask the TradingAgents LLM graph (needs API keys)")
    p_trade.add_argument("--live-i-understand-the-risk", action="store_true")

    sub.add_parser("journal", help="show performance and lessons learned")
    sub.add_parser("payout", help="grade the journal against prop-firm payout gates")

    p_bt = sub.add_parser("backtest", help="honest out-of-sample test on unseen data")
    p_bt.add_argument("--symbol", default="SPY")
    p_bt.add_argument("--period", default="3mo")
    p_bt.add_argument("--interval", default="1d")
    p_bt.add_argument("--episodes", type=int, default=40)

    p_prac = sub.add_parser(
        "practice",
        help="drill the agent across synthetic market regimes (trends, chop, "
             "flash crashes, news spikes, gaps) and report where it bleeds",
    )
    p_prac.add_argument("--scenarios", type=int, default=300,
                        help="how many synthetic sessions to run (default 300)")
    p_prac.add_argument("--episodes", type=int, default=2,
                        help="learning passes per scenario")
    p_prac.add_argument("--seed", type=int, default=0,
                        help="reproducibility seed for the synthetic tape")
    p_prac.add_argument("--save", action="store_true",
                        help="harden the LIVE Q-table in place and save it "
                             "(default: fresh agent, report only)")

    p_odds = sub.add_parser(
        "challenge-odds",
        help="Monte Carlo estimate of the odds of passing a specific "
             "prop-firm challenge, using the live-trained policy",
    )
    p_odds.add_argument("--attempts", type=int, default=300)
    p_odds.add_argument("--bars", type=int, default=3000,
                        help="synthetic bars per simulated attempt")
    p_odds.add_argument("--seed", type=int, default=0)
    p_odds.add_argument("--target", type=float, default=0.10,
                        help="challenge profit target, e.g. 0.10 = 10%%")
    p_odds.add_argument("--daily-loss", type=float, default=0.04,
                        help="daily loss limit, e.g. 0.04 = 4%%")
    p_odds.add_argument("--max-drawdown", type=float, default=0.06,
                        help="max total drawdown limit, e.g. 0.06 = 6%%")
    p_odds.add_argument("--risk-per-trade", type=float, default=0.015,
                        help="fraction of equity risked per trade, e.g. 0.015 = 1.5%%")
    p_odds.add_argument("--real", action="store_true",
                        help="use block-bootstrapped REAL market history instead "
                             "of synthetic scenarios -- an independent check, since "
                             "the synthetic tape is generated by the same functions "
                             "used to practice the agent")

    p_auto = sub.add_parser("autopilot", help="run desk cycles on a loop, hands-free")
    p_auto.add_argument("--broker", default="paper",
                        choices=["paper", "alpaca", "robinhood", "crypto", "oanda", "tradelocker", "mt5"])
    p_auto.add_argument("--interval", type=int, default=30, help="minutes between cycles")
    p_auto.add_argument("--symbols", default="",
                        help="comma-separated watchlist (default: copy-trade signals)")
    p_auto.add_argument("--llm-committee", action="store_true")
    p_auto.add_argument("--live-i-understand-the-risk", action="store_true")
    p_auto.add_argument("--day-trading", action="store_true",
                        help="use intraday candles and flatten all positions before close")
    p_auto.add_argument("--funded", action="store_true",
                        help="funded-account rules: 3%% daily loss limit, 5%% max total drawdown, "
                             "day-trading+ATR+correlation+news-blackout all on")
    p_auto.add_argument("--timeframe", default=None,
                        help="candle size for signals, e.g. 5m/15m/1h (default: 1d, or 5m if --day-trading)")
    p_auto.add_argument("--market", default=None, choices=["stocks", "forex", "crypto"],
                        help="force the trading-hours clock (default: auto-detect from broker/symbols)")
    p_auto.add_argument("--weekend-symbols", default="",
                        help="comma-separated crypto watchlist to trade over the forex-closed "
                             "weekend instead of sitting idle (default: BTC-USD,ETH-USD when "
                             "--market forex and --funded are both set)")
    p_auto.add_argument("--stock-symbols", default="",
                        help="comma-separated stock watchlist to add during the 9:30-16:00 ET "
                             "NYSE session, on top of the always-on forex/index watchlist "
                             "(default: AAPL,MSFT,NVDA,SPY,QQQ when --market forex and --funded "
                             "are both set)")

    p_watch = sub.add_parser(
        "watch", help="check YouTube/Twitter/Instagram for trade calls, auto-queue them"
    )
    p_watch.add_argument("--youtube", default="", help="channel ID or @handle")
    p_watch.add_argument("--twitter", default="", help="X/Twitter @handle")
    p_watch.add_argument("--instagram", default="", help="Instagram username")
    p_watch.add_argument("--stories", action="store_true",
                         help="check Instagram Stories instead of feed posts")
    p_watch.add_argument("--limit", type=int, default=5,
                         help="how many recent items to check per source")

    p_mirror = sub.add_parser(
        "mirror", help="record a trade call from a human you follow (IG/YT/Discord)"
    )
    p_mirror.add_argument("symbol", nargs="?", default="")
    p_mirror.add_argument("--side", default="buy", choices=["buy", "sell"])
    p_mirror.add_argument("--source", default="manual",
                          help="who called it, e.g. mambafx")
    p_mirror.add_argument("--note", default="")
    p_mirror.add_argument("--list", action="store_true", help="show pending calls")
    p_mirror.add_argument("--clear", action="store_true",
                          help="remove the pending call for SYMBOL")

    args = parser.parse_args()
    {
        "demo": cmd_demo,
        "train": cmd_train,
        "signals": cmd_signals,
        "trade": cmd_trade,
        "journal": cmd_journal,
        "payout": cmd_payout,
        "backtest": cmd_backtest,
        "practice": cmd_practice,
        "challenge-odds": cmd_challenge_odds,
        "autopilot": cmd_autopilot,
        "watch": cmd_watch,
        "mirror": cmd_mirror,
    }[args.command](args)


if __name__ == "__main__":
    main()
