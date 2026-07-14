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
        config=DeskConfig(min_copy_score=0),
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

    df = train_mod.fetch_history(args.symbol, args.period)
    from bots.learning import QTraderAgent

    agent = QTraderAgent()
    agent.load()
    stats = agent.train(df, episodes=args.episodes)
    agent.save()
    print(
        f"Trained on {args.symbol}: {stats['trades']} trades, "
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

    broker = get_broker(args.broker)
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


def cmd_journal(_args) -> None:
    from bots.journal import TradeJournal

    print(TradeJournal().summary() or "Journal is empty.")


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
    p_train.add_argument("--episodes", type=int, default=50)

    sub.add_parser("signals", help="show smart-money consensus buy signals")

    p_trade = sub.add_parser("trade", help="run one trading-desk cycle")
    p_trade.add_argument("--broker", default="paper",
                         choices=["paper", "alpaca", "robinhood", "crypto", "oanda"])
    p_trade.add_argument("--symbols", default="",
                         help="comma-separated watchlist (default: copy-trade signals)")
    p_trade.add_argument("--llm-committee", action="store_true",
                         help="also ask the TradingAgents LLM graph (needs API keys)")
    p_trade.add_argument("--live-i-understand-the-risk", action="store_true")

    sub.add_parser("journal", help="show performance and lessons learned")

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
        "mirror": cmd_mirror,
    }[args.command](args)


if __name__ == "__main__":
    main()
