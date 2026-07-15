"""Tests for the bots/ stack: journal, RL agent, paper broker, trading desk."""

import numpy as np
import pandas as pd
import pytest

from bots.brokers import PaperBroker, get_broker
from bots.copytrader import manual
from bots.journal import TradeJournal
from bots.learning import QTraderAgent
from bots.organization import DeskConfig, DeskReport, TradingDesk
from bots.risk import DrawdownGuard


def make_desk(tmp_path, broker, journal, price_df, config=None, agent=None):
    return TradingDesk(
        broker=broker,
        journal=journal,
        agent=agent or QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=config or DeskConfig(news_blackout=False, min_copy_score=0),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day_state.json")),
        manual_signals_path=str(tmp_path / "manual_signals.json"),
    )


@pytest.fixture
def price_df():
    rng = np.random.default_rng(42)
    prices = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 300)))
    return pd.DataFrame({"close": prices})


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(path=str(tmp_path / "journal.json"))


def test_journal_records_and_learns(journal):
    # Five losing trades on one setup should get it blocked.
    for _ in range(5):
        record = journal.open_trade("TEST", "long", 10, 100.0, setup="bad-setup")
        journal.close_trade(record.trade_id, 95.0)
    winner = journal.open_trade("TEST", "long", 10, 100.0, setup="good-setup")
    journal.close_trade(winner.trade_id, 110.0)

    stats = journal.setup_stats()
    assert stats["bad-setup"]["trades"] == 5
    assert stats["bad-setup"]["avg_pnl"] < 0
    assert journal.should_avoid("bad-setup")
    assert not journal.should_avoid("good-setup")


def test_journal_persists(tmp_path):
    path = str(tmp_path / "journal.json")
    journal = TradeJournal(path=path)
    record = journal.open_trade("AAPL", "long", 1, 150.0, setup="s")
    journal.close_trade(record.trade_id, 160.0)

    reloaded = TradeJournal(path=path)
    assert len(reloaded.trades) == 1
    assert list(reloaded.trades.values())[0].pnl == pytest.approx(10.0)


def test_agent_trains_and_signals(price_df, tmp_path):
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    stats = agent.train(price_df, episodes=10)
    assert agent.trained_episodes == 10
    assert stats["trades"] >= 0
    assert agent.signal(price_df) in ("buy", "sell", "hold")

    agent.save()
    fresh = QTraderAgent(model_path=str(tmp_path / "q.json"))
    assert fresh.load()
    assert fresh.q == agent.q


def test_agent_accepts_yfinance_columns(price_df, tmp_path):
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    upper = price_df.rename(columns={"close": "Close"})
    assert agent.signal(upper) in ("buy", "sell", "hold")


def test_paper_broker_round_trip(tmp_path):
    broker = PaperBroker(
        starting_cash=1_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"XYZ": 10.0},
    )
    result = broker.buy("XYZ", 50)
    assert result.ok and result.fill_price == 10.0
    assert broker.cash() == pytest.approx(500.0)
    assert broker.positions() == {"XYZ": 50}

    # can't oversell or overspend
    assert not broker.sell("XYZ", 100).ok
    assert not broker.buy("XYZ", 1_000).ok

    result = broker.sell("XYZ", 50)
    assert result.ok
    assert broker.cash() == pytest.approx(1_000.0)
    assert broker.positions() == {}


def test_get_broker_factory(tmp_path):
    broker = get_broker("paper", state_path=str(tmp_path / "acct.json"))
    assert broker.name == "paper" and broker.is_paper
    with pytest.raises(ValueError):
        get_broker("etrade")


def test_desk_cycle_buys_and_respects_lessons(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": float(price_df["close"].iloc[-1])},
    )
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.train(price_df, episodes=5)
    desk = make_desk(tmp_path, broker, journal, price_df, agent=agent)
    report = desk.run_once(symbols=["DEMO"])
    actions = {a.symbol: a for a in report.actions}
    assert "DEMO" in actions
    assert actions["DEMO"].action in ("buy", "skip")
    if actions["DEMO"].action == "buy":
        assert broker.positions().get("DEMO", 0) > 0
        assert journal.open_position_for("DEMO") is not None
        # risk sizing: no more than 15% of equity in one name
        cost = actions["DEMO"].quantity * broker.price("DEMO")
        assert cost <= 10_000 * 0.15 * 1.01


def test_desk_stop_loss_closes_position(price_df, tmp_path, journal):
    entry_price = 100.0
    crashed = 90.0  # -10% -> beyond the 5% stop
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": entry_price, "UNRELATED": 50.0},
    )
    assert broker.buy("DEMO", 10).ok
    journal.open_trade("DEMO", "long", 10, entry_price, setup="test")
    broker.price_overrides["DEMO"] = crashed

    desk = make_desk(
        tmp_path, broker, journal, price_df,
        # risk budget 0 -> no new entries, keeps the test offline
        config=DeskConfig(news_blackout=False, min_copy_score=99, risk_per_trade_pct=0.0),
    )
    report = desk.run_once(symbols=["UNRELATED"])
    sells = [a for a in report.actions if a.action == "sell"]
    assert sells and sells[0].ok
    assert broker.positions() == {}
    closed = [t for t in journal.trades.values() if not t.is_open]
    assert closed and closed[0].pnl == pytest.approx((crashed - entry_price) * 10)


def test_circuit_breaker_blocks_new_entries(price_df, tmp_path, journal):
    guard = DrawdownGuard(max_daily_loss_pct=0.05, state_path=str(tmp_path / "day.json"))
    assert guard.check(10_000)[0] is False  # records the day-start baseline
    halted, msg = guard.check(9_400)  # down 6% -> breaker trips
    assert halted and "CIRCUIT BREAKER" in msg

    broker = PaperBroker(
        starting_cash=9_400,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0),
        history_fn=lambda _s: price_df,
        guard=guard,
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("CIRCUIT BREAKER" in n for n in report.notes)


def test_mirror_signal_executes_under_risk_rules_and_consumes(price_df, tmp_path, journal):
    sig_path = str(tmp_path / "manual_signals.json")
    manual.add_signal("DEMO", side="buy", source="mambafx", path=sig_path)

    last = float(price_df["close"].iloc[-1])
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": last},
    )
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=99),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=sig_path,
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    record = journal.open_position_for("DEMO")
    assert record is not None and record.setup == "mirror:mambafx"
    assert manual.pending_signals(sig_path) == []  # consumed after execution


def test_risk_per_trade_sizing(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        # risking 0.5% with a 5% stop -> position value = 10000*0.005/0.05 = 1000
        config=DeskConfig(news_blackout=False, min_copy_score=0, risk_per_trade_pct=0.005, stop_loss_pct=0.05),
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    assert buys[0].quantity * 100.0 == pytest.approx(1_000.0, rel=0.01)


def test_desk_skips_entry_with_pending_order(price_df, tmp_path, journal):
    class FakePendingBroker(PaperBroker):
        def has_pending_order(self, symbol):
            return symbol.upper() == "DEMO"

    broker = FakePendingBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(tmp_path, broker, journal, price_df, config=DeskConfig(news_blackout=False, min_copy_score=0))
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("pending fill" in a.reason for a in report.actions if a.symbol == "DEMO")
    assert broker.positions() == {}


def test_max_positions_counts_pending_journal_entries(price_df, tmp_path, journal):
    # Simulate a broker that queues orders instead of filling them (like a
    # stock order placed outside market hours): buy() succeeds and the
    # journal opens a trade, but broker.positions() stays empty.
    class QueuedFillBroker(PaperBroker):
        def positions(self):
            return {}

    broker = QueuedFillBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={f"SYM{i}": 100.0 for i in range(6)},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_positions=5),
    )
    report = desk.run_once(symbols=[f"SYM{i}" for i in range(6)])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 5, report.describe()
    assert any(
        a.action == "skip" and "max positions" in a.reason for a in report.actions
    )
    open_symbols = {t.symbol for t in journal.trades.values() if t.is_open}
    assert len(open_symbols) == 5


def test_intraday_state_features(tmp_path):
    from bots.learning.agent import extract_state

    rng = np.random.default_rng(3)
    frames = []
    for day in ("2026-07-08", "2026-07-09"):
        idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min")
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 78)))
        frames.append(
            pd.DataFrame(
                {"open": px, "high": px * 1.001, "low": px * 0.999, "close": px,
                 "volume": rng.integers(1000, 5000, 78)},
                index=idx,
            )
        )
    intraday = pd.concat(frames)
    state = extract_state(intraday, 100, holding=False)
    for part in ("trend-", "rsi-", "vwap-", "orb-", "tod-", "pos-out"):
        assert part in state, state
    # first bar of a session is inside the opening range and the open hour
    state_open = extract_state(intraday, 78, holding=False)
    assert "orb-in" in state_open and "tod-open" in state_open

    daily = pd.DataFrame({"close": 100 + np.arange(60.0)})
    daily_state = extract_state(daily, 50, holding=True)
    assert "vwap-" not in daily_state  # daily states unchanged for old Q-tables
    assert daily_state.endswith("pos-in")


def test_daily_trade_cap(price_df, tmp_path, journal):
    for i in range(3):
        journal.open_trade(f"OLD{i}", "long", 1, 10.0, setup="s")  # opened today
    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"NEW1": 10.0, "NEW2": 10.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_positions=99, max_trades_per_day=4),
    )
    report = desk.run_once(symbols=["NEW1", "NEW2"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    capped = [a for a in report.actions if "daily trade cap" in a.reason]
    assert len(buys) == 1 and len(capped) == 1, report.describe()


def test_flatten_all_closes_every_position(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAA": 50.0, "BBB": 20.0},
    )
    assert broker.buy("AAA", 10).ok
    assert broker.buy("BBB", 25).ok
    journal.open_trade("AAA", "long", 10, 50.0, setup="daytrade")
    journal.open_trade("BBB", "long", 25, 20.0, setup="daytrade")

    desk = make_desk(tmp_path, broker, journal, price_df)
    report = desk.flatten_all(reason="end of day")
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert {a.symbol for a in sells} == {"AAA", "BBB"}
    assert all("end of day" in a.reason for a in sells)
    assert broker.positions() == {}
    assert all(not t.is_open for t in journal.trades.values())


def test_minutes_to_stock_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.autopilot import minutes_to_stock_close

    ny = ZoneInfo("America/New_York")
    assert minutes_to_stock_close(datetime(2026, 7, 14, 15, 50, tzinfo=ny)) == 10
    assert minutes_to_stock_close(datetime(2026, 7, 14, 12, 0, tzinfo=ny)) == 240
    assert minutes_to_stock_close(datetime(2026, 7, 14, 20, 0, tzinfo=ny)) is None
    assert minutes_to_stock_close(datetime(2026, 7, 18, 12, 0, tzinfo=ny)) is None  # Saturday


def test_autopilot_flattens_before_close(price_df, tmp_path, monkeypatch):
    import bots.autopilot as ap
    from bots.organization import DeskConfig, TradingDesk
    from bots.risk import DrawdownGuard

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAA": 50.0},
    )
    assert broker.buy("AAA", 10).ok
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    journal.open_trade("AAA", "long", 10, 50.0, setup="daytrade")

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, day_trading=True),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
    )

    calls = []
    monkeypatch.setattr(desk, "flatten_all", lambda **kw: calls.append("flatten") or DeskReport())
    monkeypatch.setattr(desk, "run_once", lambda **kw: calls.append("run_once") or DeskReport())
    monkeypatch.setattr(ap, "market_is_open", lambda _m, now=None: True)
    # 10 minutes to close, under the default 15-minute flatten window
    monkeypatch.setattr(ap, "minutes_to_stock_close", lambda now=None: 10)
    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)

    ap.run_autopilot(broker_name="paper", interval_minutes=1, desk=desk, max_cycles=1)
    assert calls == ["flatten"]


def test_buy_bracket_fallback_and_desk_uses_it(price_df, tmp_path, journal):
    calls = {}

    class BracketBroker(PaperBroker):
        def buy_bracket(self, symbol, quantity, stop_loss_pct, take_profit_pct):
            calls["args"] = (symbol, stop_loss_pct, take_profit_pct)
            return super().buy(symbol, quantity)

    broker = BracketBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, stop_loss_pct=0.015, take_profit_pct=0.03),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert [a for a in report.actions if a.action == "buy" and a.ok], report.describe()
    assert calls["args"] == ("DEMO", 0.015, 0.03)

    # base-class fallback: plain broker still works through buy_bracket
    plain = PaperBroker(
        starting_cash=1_000, state_path=str(tmp_path / "b.json"),
        price_overrides={"XYZ": 10.0},
    )
    assert plain.buy_bracket("XYZ", 5, 0.015, 0.03).ok


def test_reconcile_closes_orphaned_journal_entries(price_df, tmp_path, journal):
    # journal thinks we hold GONE, but the broker has no such position and no
    # pending order -> a bracket stop/target must have filled between cycles
    journal.open_trade("GONE", "long", 10, 100.0, setup="daytrade")
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"GONE": 103.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=99, max_positions=0),
    )
    report = desk.run_once(symbols=["GONE"])
    reconciled = [a for a in report.actions if "reconciled" in a.reason]
    assert reconciled, report.describe()
    record = [t for t in journal.trades.values() if t.symbol == "GONE"][0]
    assert not record.is_open and record.pnl == pytest.approx(30.0)


def test_performance_metrics(journal):
    # 1 winner (+100), 2 losers (-30, -20) -> profit factor 100/50=2.0,
    # expectancy (100-30-20)/3=+16.67, drawdown = worst peak-to-trough dip
    for pnl_pairs in [(100.0, 200.0), (-30.0, 170.0), (-20.0, 150.0)]:
        entry, exit_ = 100.0, 100.0 + pnl_pairs[0]
        t = journal.open_trade("X", "long", 1, entry, setup="s")
        journal.close_trade(t.trade_id, exit_)
    m = journal.performance_metrics()
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["expectancy"] == pytest.approx(50.0 / 3)
    assert m["max_drawdown"] == pytest.approx(50.0)  # peak 100 -> trough 50


def test_performance_metrics_all_wins_is_infinite_profit_factor(journal):
    t = journal.open_trade("X", "long", 1, 100.0, setup="s")
    journal.close_trade(t.trade_id, 110.0)
    assert journal.performance_metrics()["profit_factor"] == float("inf")


def test_extract_calls_buy_and_sell():
    from bots.social.signals import extract_calls

    calls = extract_calls("Just went LONG on $AAPL here, also might SELL TSLA soon")
    by_symbol = {c.symbol: c.side for c in calls}
    assert by_symbol.get("AAPL") == "buy"
    assert by_symbol.get("TSLA") == "sell"


def test_extract_calls_forex_and_stopwords():
    from bots.social.signals import extract_calls

    calls = extract_calls("BUY EURUSD now, I think it goes up. Also SHORT GBPJPY")
    symbols = {c.symbol for c in calls}
    assert "EURUSD" in symbols
    assert "GBPJPY" in symbols
    assert "I" not in symbols  # stopword filtered, not a real ticker


def test_extract_calls_no_false_positive_on_plain_text():
    from bots.social.signals import extract_calls

    assert extract_calls("Market was choppy today, nothing exciting happened.") == []


def test_out_of_sample_backtest(price_df):
    from bots.learning.backtest import run_backtest

    result = run_backtest(price_df, train_fraction=0.7, episodes=5)
    assert result["train_bars"] + result["test_bars"] == len(price_df)
    assert result["test_bars"] < result["train_bars"]
    assert "edge_vs_buy_hold_pct" in result
    assert result["edge_vs_buy_hold_pct"] == pytest.approx(
        result["agent_return_pct"] - result["buy_hold_return_pct"]
    )


def test_market_hours_clock():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.autopilot import market_is_open

    ny = ZoneInfo("America/New_York")
    tuesday_noon = datetime(2026, 7, 14, 12, 0, tzinfo=ny)
    tuesday_night = datetime(2026, 7, 14, 20, 0, tzinfo=ny)
    saturday = datetime(2026, 7, 18, 12, 0, tzinfo=ny)
    sunday_evening = datetime(2026, 7, 19, 18, 0, tzinfo=ny)

    assert market_is_open("stocks", tuesday_noon)
    assert not market_is_open("stocks", tuesday_night)
    assert not market_is_open("stocks", saturday)
    assert market_is_open("forex", tuesday_night)
    assert not market_is_open("forex", saturday)
    assert market_is_open("forex", sunday_evening)
    assert market_is_open("crypto", saturday)


def test_autopilot_runs_cycles_offline(price_df, tmp_path, monkeypatch, capsys):
    import bots.autopilot as ap

    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ap, "market_is_open", lambda _m, now=None: True)

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": float(price_df["close"].iloc[-1])},
    )
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    desk = make_desk(tmp_path, broker, journal, price_df)

    ap.run_autopilot(
        broker_name="paper", interval_minutes=1, symbols=["DEMO"],
        max_cycles=2, desk=desk,
    )
    out = capsys.readouterr().out
    assert "Autopilot started" in out
    assert "cycle 2" in out


def test_oanda_symbol_normalization():
    from bots.brokers.oanda import _instrument

    assert _instrument("EURUSD") == "EUR_USD"
    assert _instrument("eur/usd") == "EUR_USD"
    assert _instrument("EUR_USD") == "EUR_USD"
    assert _instrument("GBPJPY") == "GBP_JPY"


def test_tradelocker_lot_conversion():
    from bots.brokers.tradelocker_broker import _units_to_lots

    # forex: units -> lots at 100k/lot, floored to 2dp, 0.01 minimum
    assert _units_to_lots("EURUSD", 100_000) == 1.0
    assert _units_to_lots("EURUSD", 1_500) == pytest.approx(0.01)
    assert _units_to_lots("EURUSD", 250_000) == pytest.approx(2.5)
    assert _units_to_lots("GBPJPY", 100) == 0.01  # clamped to min lot
    # non-forex instruments pass through unchanged
    assert _units_to_lots("NAS100", 2.5) == 2.5
    assert _units_to_lots("BTCUSD.X", 0.3) == 0.3


def test_news_guard_blocks_around_high_impact_events():
    from datetime import datetime, timedelta, timezone

    from bots.newsguard import NewsGuard

    now = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    events = [
        {"title": "CPI y/y", "country": "USD", "impact": "High",
         "date": (now + timedelta(minutes=5)).isoformat()},
        {"title": "Low thing", "country": "USD", "impact": "Low",
         "date": now.isoformat()},
        {"title": "EUR news", "country": "EUR", "impact": "High",
         "date": now.isoformat()},
    ]
    guard = NewsGuard(currencies=("USD",), fetch_fn=lambda: events)
    blocked, why = guard.blackout(now=now)
    assert blocked and "CPI" in why

    # 30 minutes later: outside the window
    blocked, _ = guard.blackout(now=now + timedelta(minutes=30))
    assert not blocked

    # EUR-only guard ignores the USD event
    eur_guard = NewsGuard(currencies=("CHF",), fetch_fn=lambda: events)
    assert not eur_guard.blackout(now=now)[0]


def test_news_guard_fails_safe_on_broken_feed():
    from bots.newsguard import NewsGuard

    def broken():
        raise RuntimeError("feed down")

    guard = NewsGuard(fetch_fn=broken)
    blocked, why = guard.blackout()
    assert not blocked  # never lock the desk because a feed is down


def test_correlation_guard_caps_cluster_exposure(price_df, tmp_path, journal):
    # 3 mega-cap tech candidates, cluster cap 2 -> third is skipped even
    # though position slots remain
    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_positions=99, max_per_correlation_group=2),
    )
    report = desk.run_once(symbols=["AAPL", "MSFT", "NVDA"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    skipped = [a for a in report.actions if "correlation guard" in a.reason]
    assert len(buys) == 2 and len(skipped) == 1, report.describe()


def test_breakeven_stop_after_1r(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    record = journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)

    # +2% (>= 1R of 1.5%, below 3% target) -> stop arms at breakeven, holds
    broker.price_overrides["DEMO"] = 102.0
    report1 = desk.run_once(symbols=[])
    assert any("breakeven" in n for n in report1.notes), report1.describe()
    assert "breakeven-armed" in journal.trades[record.trade_id].tags
    assert broker.positions() == {"DEMO": 10}

    # pullback to entry -> risk-free exit at ~zero instead of riding to -1.5%
    broker.price_overrides["DEMO"] = 99.9
    report2 = desk.run_once(symbols=[])
    sells = [a for a in report2.actions if a.action == "sell" and a.ok]
    assert sells and "breakeven" in sells[0].reason, report2.describe()
    closed = journal.trades[record.trade_id]
    assert not closed.is_open and closed.pnl == pytest.approx(-1.0, abs=0.01)


def test_loss_streak_rule_blocks_entries(price_df, tmp_path, journal):
    for i in range(3):
        t = journal.open_trade(f"L{i}", "long", 1, 100.0, setup="s")
        journal.close_trade(t.trade_id, 99.0)  # 3 straight losses today
    assert journal.consecutive_losses_today() == 3

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_consecutive_losses=3),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("loss-streak" in n for n in report.notes)

    # a winner resets the streak
    w = journal.open_trade("W", "long", 1, 100.0, setup="s")
    journal.close_trade(w.trade_id, 105.0)
    assert journal.consecutive_losses_today() == 0


def test_atr_stops_size_by_volatility(tmp_path, journal):
    import numpy as np
    import pandas as pd

    def make_ohlc(vol):
        rng = np.random.default_rng(9)
        close = 100 + np.cumsum(rng.normal(0, vol, 100))
        return pd.DataFrame({
            "high": close + vol * 2, "low": close - vol * 2, "close": close,
        })

    calm, wild = make_ohlc(0.05), make_ohlc(2.0)

    from bots.organization import atr_pct

    calm_atr, wild_atr = atr_pct(calm), atr_pct(wild)
    assert calm_atr and wild_atr and wild_atr > calm_atr * 5

    # desk sizing: wild instrument gets a wider stop and a smaller position
    quantities = {}
    for name, df in (("CALM", calm), ("WILD", wild)):
        broker = PaperBroker(
            starting_cash=100_000,
            state_path=str(tmp_path / f"acct-{name}.json"),
            price_overrides={name: 100.0},
        )
        desk = make_desk(
            tmp_path, broker, journal, df,
            config=DeskConfig(news_blackout=False, min_copy_score=0,
                              atr_stops=True, max_per_correlation_group=0,
                              max_position_pct=1.0),
        )
        report = desk.run_once(symbols=[name])
        buys = [a for a in report.actions if a.action == "buy" and a.ok]
        if buys:
            quantities[name] = buys[0].quantity
            record = [t for t in journal.trades.values() if t.symbol == name][-1]
            assert record.stop_pct is not None
    if "CALM" in quantities and "WILD" in quantities:
        assert quantities["WILD"] < quantities["CALM"]


def test_max_drawdown_guard_halts_and_stays_halted(tmp_path):
    from bots.risk import MaxDrawdownGuard

    guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    assert guard.check(10_000)[0] is False  # peak = 10000
    assert guard.check(10_500)[0] is False  # new peak = 10500, no drawdown
    halted, msg = guard.check(9_900)  # down 5.7% from peak 10500 -> breach
    assert halted and "HALTED" in msg
    # stays halted even if equity recovers -- funded accounts don't un-terminate
    still_halted, _ = guard.check(10_600)
    assert still_halted


def test_funded_account_config_matches_screenshot_numbers():
    from bots.organization import funded_account_config

    cfg = funded_account_config()
    assert cfg.max_daily_loss_pct == 0.03
    assert cfg.max_total_drawdown_pct == 0.05
    assert cfg.day_trading and cfg.atr_stops and cfg.news_blackout


def test_desk_halts_on_total_drawdown_breach(price_df, tmp_path, journal):
    from bots.risk import MaxDrawdownGuard

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    dd_guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    dd_guard.check(10_000)  # establish peak
    dd_guard.check(9_400)  # pre-breach the guard directly (down 6%)

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
        max_drawdown_guard=dd_guard,
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("HALTED" in n for n in report.notes)


def test_daily_profit_target_cashes_out_and_stops(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=5_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"WIN": 100.0, "NEXT": 50.0},
    )
    guard = DrawdownGuard(state_path=str(tmp_path / "day.json"))
    guard.check(5_000)  # record day-start baseline

    # open a position that then runs up so equity = 5150 (+3% day)
    assert broker.buy("WIN", 10).ok
    journal.open_trade("WIN", "long", 10, 100.0, setup="daytrade")
    broker.price_overrides["WIN"] = 115.0  # equity 4000 cash + 1150 = 5150

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          daily_profit_target_pct=0.02, take_profit_pct=0.99),
        history_fn=lambda _s: price_df,
        guard=guard,
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    report = desk.run_once(symbols=["NEXT"])
    # cashed out the winner, took no new trades
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert sells and "profit target" in sells[0].reason
    assert broker.positions() == {}
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("done for the day" in n for n in report.notes)

    # next cycle same day: realized gain keeps it stopped
    report2 = desk.run_once(symbols=["NEXT"])
    assert not [a for a in report2.actions if a.action == "buy"]


def test_funded_config_includes_profit_target():
    from bots.organization import funded_account_config

    assert funded_account_config().daily_profit_target_pct == 0.02


def test_adx_regime_filter_skips_choppy_markets(tmp_path, journal):
    import numpy as np
    import pandas as pd

    # trending: steady climb -> high ADX. choppy: tight oscillation -> low ADX.
    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    chop_close = pd.Series(100 + 0.3 * np.sin(np.arange(n)))
    choppy = pd.DataFrame({
        "high": chop_close + 0.1, "low": chop_close - 0.1, "close": chop_close,
    })

    from bots.organization import adx_value

    adx_trend, adx_chop = adx_value(trending), adx_value(choppy)
    assert adx_trend is not None and adx_chop is not None
    assert adx_trend > 25 and adx_chop < 20, (adx_trend, adx_chop)

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"CHOP": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, choppy,
        config=DeskConfig(news_blackout=False, min_copy_score=0, min_adx=20.0),
    )
    report = desk.run_once(symbols=["CHOP"])
    skips = [a for a in report.actions if "regime filter" in a.reason]
    assert skips and not [a for a in report.actions if a.action == "buy"], report.describe()


def test_funded_config_includes_regime_filter():
    from bots.organization import funded_account_config

    assert funded_account_config().min_adx == 20.0
